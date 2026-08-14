"""
SVN 只读客户端：为 Phase 1 提供「按需拉取」能力。

移植自 smartdiff/svn_helper.py 的只读查询函数（已剔除工作副本 / 冲突相关函数），
新增：
- (path, rev) 内容缓存层：SVN 内容永久不可变，可无限期缓存，二次运行基本不走网络（PRD §A2）。
- peg revision 支持：路径在区间内被 rename 时仍可拉到正确内容。
- 并发拉取 + 进度回调：中等规模必需。

对外主入口：fetch_revisions(branch_url, rev_from, rev_to) -> (revisions, meta)
产出结构与 core.attributor.run() 完全兼容（见 core/attributor.py 契约）：
- revisions[0] 为 baseline：含区间内全部 CSV 的完整快照，author=None。
- 其后每条仅含该 revision 变更的 CSV（新内容；删除的给 None），路径用正斜杠。
"""
import os
import re
import json
import hashlib
import urllib.parse
import subprocess
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable, Dict, List, Tuple


# ----------------------------------------------------------------------------
# 底层 plumbing（移植自 smartdiff/svn_helper.py，已验证可用）
# ----------------------------------------------------------------------------
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # subprocess.CREATE_NO_WINDOW


def _hidden_kwargs() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


def _find_svn() -> Optional[str]:
    """Auto-detect svn CLI path (incl. TortoiseSVN)."""
    for candidate in ["svn"]:
        try:
            r = subprocess.run([candidate, "--version", "--quiet"],
                               capture_output=True, timeout=5, **_hidden_kwargs())
            if r.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    for tp in [r"C:\Program Files\TortoiseSVN\bin\svn.exe",
              r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe"]:
        if os.path.isfile(tp):
            try:
                r = subprocess.run([tp, "--version", "--quiet"],
                                   capture_output=True, timeout=5, **_hidden_kwargs())
                if r.returncode == 0:
                    return tp
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    return None


def _decode_output(data: bytes) -> str:
    """Decode subprocess output: UTF-8 -> GBK -> replace (Chinese Windows)."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        pass
    return data.decode("utf-8", errors="replace")


def _run(*args, cwd=None, timeout=30) -> Tuple[int, str, str]:
    svn = _find_svn()
    if not svn:
        return (-1, "", "svn not found")
    try:
        r = subprocess.run([svn, "--non-interactive"] + list(args), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           cwd=cwd, **_hidden_kwargs())
        return (r.returncode, _decode_output(r.stdout), _decode_output(r.stderr))
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout")
    except Exception as e:  # noqa: BLE001
        return (-1, "", str(e))


def _run_raw(*args, cwd=None, timeout=30) -> Tuple[int, bytes, str]:
    svn = _find_svn()
    if not svn:
        return (-1, b"", "svn not found")
    try:
        r = subprocess.run([svn, "--non-interactive"] + list(args), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           cwd=cwd, **_hidden_kwargs())
        return (r.returncode, r.stdout, _decode_output(r.stderr))
    except subprocess.TimeoutExpired:
        return (-1, b"", "timeout")
    except Exception as e:  # noqa: BLE001
        return (-1, b"", str(e))


def _is_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "svn://", "svn+ssh://", "file://"))


# ----------------------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------------------
class SVNClient:
    def __init__(self, cache_dir: Optional[str] = None, timeout: int = 60,
                 max_workers: int = 8):
        self.timeout = timeout
        self.max_workers = max_workers
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._mem: Dict[Tuple[str, object], Optional[bytes]] = {}
        self._cache_counters = {
            "memory_hits": 0,
            "disk_hits": 0,
            "misses": 0,
            "writes": 0,
        }

    def cache_metrics(self) -> dict:
        with self._lock:
            return {
                **self._cache_counters,
                "memory_entries": len(self._mem),
            }

    def clear_memory_cache(self) -> int:
        with self._lock:
            count = len(self._mem)
            self._mem.clear()
            return count

    # ---- 可用性 ----
    def available(self) -> bool:
        return _find_svn() is not None

    # ---- 元数据 ----
    def info(self, url: str) -> Optional[dict]:
        rc, out, _ = _run("info", "--xml", url, timeout=self.timeout)
        if rc != 0:
            return None
        try:
            root = ET.fromstring(out)
        except ET.ParseError:
            return None
        entry = root.find(".//entry")
        if entry is None:
            return None
        url2 = entry.findtext("url", "") or ""
        root2 = entry.findtext("repository/root", "") or ""
        commit = entry.find("commit")
        lcr = commit.get("revision", "") if commit is not None else ""
        lca = entry.findtext("commit/author", "") if commit is not None else ""
        lcd = entry.findtext("commit/date", "") if commit is not None else ""
        repo_rel = ""
        if url2 and root2 and url2.startswith(root2):
            repo_rel = urllib.parse.unquote(url2[len(root2):])
        return {"url": url2, "root": root2, "revision": entry.get("revision", ""),
                "last_changed_rev": lcr, "last_changed_author": lca,
                "last_changed_date": lcd, "repo_rel": repo_rel}

    def head_revision(self, url: str) -> Optional[int]:
        info = self.info(url)
        if info and info.get("last_changed_rev"):
            try:
                return int(info["last_changed_rev"])
            except ValueError:
                return None
        return None

    # ---- 枚举 ----
    def list_csv(self, url: str, rev: object) -> List[str]:
        """列出 rev 时刻、url 下的全部 CSV 相对路径（如 table/ArenaPeak_Base.csv）。"""
        target = f"{url}@{rev}"
        rc, out, _ = _run("list", "-R", "--xml", target, timeout=self.timeout)
        if rc != 0:
            return []
        try:
            root = ET.fromstring(out)
        except ET.ParseError:
            return []
        res = []
        for e in root.iter("entry"):
            if e.get("kind") != "file":
                continue
            name = e.findtext("name") or e.get("path") or ""
            if name.lower().endswith(".csv"):
                res.append(name)
        return res

    def log_range(self, url: str, rev_from, rev_to, stop_on_copy: bool = True) -> List[dict]:
        """返回 [rev_from, rev_to] 区间内（升序）的提交，每条含 paths（repo 绝对路径）。"""
        args = ["log", "-r", f"{rev_from}:{rev_to}", "--xml", "-v"]
        if stop_on_copy:
            args.append("--stop-on-copy")
        args.append(url)
        rc, out, _ = _run(*args, timeout=max(self.timeout, 120))
        if rc != 0:
            return []
        try:
            root = ET.fromstring(out)
        except ET.ParseError:
            return []
        entries = []
        for le in root.iter("logentry"):
            rev = le.get("revision", "")
            paths = []
            pe = le.find("paths")
            if pe is not None:
                for p in pe.findall("path"):
                    paths.append({"path": p.text or "", "action": p.get("action", "")})
            entries.append({
                "revision": int(rev) if rev.isdigit() else rev,
                "author": le.findtext("author", "") or "",
                "date": le.findtext("date", "") or "",
                "message": (le.findtext("msg", "") or "").strip(),
                "paths": paths,
            })
        entries.sort(key=lambda e: e["revision"] if isinstance(e["revision"], int) else 0)
        return entries

    # ---- 内容拉取（带缓存 + peg + 并发） ----
    def _cache_path(self, url: str, rev: object) -> str:
        h = hashlib.md5((f"{url}#{rev}").encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"rev_{rev}__{h}.bin")

    def _cat_cached(self, url: str, rev: object, peg: object) -> Optional[bytes]:
        return self._cat_cached_with_source(url, rev, peg)[0]

    def _cat_cached_with_source(
        self,
        url: str,
        rev: object,
        peg: object,
    ) -> Tuple[Optional[bytes], str]:
        key = (url, rev)
        with self._lock:
            if key in self._mem:
                self._cache_counters["memory_hits"] += 1
                return self._mem[key], "memory_cache"
        if self.cache_dir:
            p = self._cache_path(url, rev)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    data = f.read()
                with self._lock:
                    self._mem[key] = data
                    self._cache_counters["disk_hits"] += 1
                return data, "disk_cache"
        with self._lock:
            self._cache_counters["misses"] += 1
        data = self._cat(url, rev, peg)
        if data is not None:
            if self.cache_dir:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(self._cache_path(url, rev), "wb") as f:
                    f.write(data)
                with self._lock:
                    self._cache_counters["writes"] += 1
            with self._lock:
                self._mem[key] = data
        return data, "svn_cat" if data is not None else "missing"

    def _cat(self, url: str, rev: object, peg: object) -> Optional[bytes]:
        target = f"{url}@{peg}" if peg is not None else url
        rc, out, _ = _run_raw("cat", "-r", str(rev), target, timeout=self.timeout)
        return out if rc == 0 else None

    def _cat_many(self, tasks: List[Tuple[str, str, object, object]],
                  progress: Optional[Callable] = None) -> Dict[str, Optional[bytes]]:
        results: Dict[str, Optional[bytes]] = {}
        total = len(tasks)
        done = 0
        if not tasks:
            return results
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(self._cat_cached, u, rv, pg): k for k, u, rv, pg in tasks}
            for f in as_completed(futs):
                key = futs[f]
                try:
                    results[key] = f.result()
                except Exception:  # noqa: BLE001
                    results[key] = None
                done += 1
                if progress:
                    progress(done, total, "拉取文件内容")
        return results

    # ---- 路径处理 ----
    @staticmethod
    def _strip_branch(abs_path: str, repo_rel: str) -> str:
        if repo_rel and abs_path.startswith(repo_rel + "/"):
            return abs_path[len(repo_rel) + 1:]
        if abs_path.startswith("/"):
            return abs_path[1:]
        return abs_path

    @staticmethod
    def _is_csv(rel: str, csv_filter: Optional[Callable]) -> bool:
        if not rel.lower().endswith(".csv"):
            return False
        if csv_filter and not csv_filter(rel):
            return False
        return True

    # ---- 主入口 ----
    def fetch_revisions(self, branch_url: str, rev_from, rev_to,
                        csv_filter: Optional[Callable] = None,
                        progress: Optional[Callable] = None) -> Tuple[list, dict]:
        """拉取 [rev_from, rev_to] 区间内所有 CSV 的变更，产出与 attributor 兼容的 revisions。

        语义：区间为 (rev_from, rev_to]，即 rev_from 作为基线快照，其后每次提交归因。
        """
        # 解析 HEAD
        if rev_to in (None, "HEAD", "head"):
            rev_to = self.head_revision(branch_url) or rev_to
        if rev_from in (None, "HEAD", "head"):
            # 若 from 也是 HEAD，退化为单点；通常 from 为给定基线
            rev_from = self.head_revision(branch_url)

        info = self.info(branch_url) or {}
        repo_rel = info.get("repo_rel", "")

        # 基线 CSV 列表 + 内容
        base_rels = [r for r in self.list_csv(branch_url, rev_from)
                     if self._is_csv(r, csv_filter)]
        tasks: List[Tuple[str, str, object, object]] = [
            ("base:" + rel, branch_url + "/" + rel, rev_from, rev_from) for rel in base_rels
        ]

        logs = self.log_range(branch_url, rev_from, rev_to)
        base_meta = next((l for l in logs if l["revision"] == rev_from), {})
        rev_changed: Dict[object, Dict[str, str]] = {}
        for le in logs:
            rv = le["revision"]
            if rv == rev_from:
                continue
            if isinstance(rv, int) and isinstance(rev_to, int) and rv > rev_to:
                continue
            rels: Dict[str, str] = {}
            for p in le["paths"]:
                rel = self._strip_branch(p["path"], repo_rel)
                if not rel or not self._is_csv(rel, csv_filter):
                    continue
                rels[rel] = p["action"]
            if rels:
                rev_changed[rv] = rels
                for rel, action in rels.items():
                    if action == "D":
                        continue
                    tasks.append((f"{rv}:{rel}", branch_url + "/" + rel, rv, rv))

        data = self._cat_many(tasks, progress)

        base_files: Dict[str, Optional[bytes]] = {rel: data.get("base:" + rel)
                                                  for rel in base_rels}
        revisions = [{
            "revision": rev_from, "author": None,
            "date": base_meta.get("date", ""),
            "message": "基线", "files": base_files,
        }]
        for rv in sorted(rev_changed.keys(),
                         key=lambda x: x if isinstance(x, int) else 0):
            files: Dict[str, Optional[bytes]] = {}
            for rel, action in rev_changed[rv].items():
                if action == "D":
                    files[rel] = None
                else:
                    files[rel] = data.get(f"{rv}:{rel}")
            meta = next((l for l in logs if l["revision"] == rv), {})
            revisions.append({
                "revision": rv, "author": meta.get("author"),
                "date": meta.get("date", ""), "message": meta.get("message", ""),
                "files": files,
            })

        meta = {"repo_url": branch_url, "rev_from": rev_from, "rev_to": rev_to,
                "blind_spot": "本报告基于导出 CSV 生成；CSV 未包含的列（导出端=None，"
                              "如中文备注/作者列）其改动不会被检出。"}
        return revisions, meta


def fetch(branch_url: str, rev_from, rev_to, cache_dir: Optional[str] = ".cache/svn",
          progress: Optional[Callable] = None) -> Tuple[list, dict]:
    """便捷函数：用默认缓存目录直接拉取。"""
    return SVNClient(cache_dir=cache_dir).fetch_revisions(
        branch_url, rev_from, rev_to, progress=progress)
