(() => {
  const SCHEMA_VERSION = "m2.diff.v1";
  const WORKBOOK_STATES = {
    unchanged: "diff_empty",
    modified: "diff_ready",
    partial: "diff_error",
    failed: "diff_error",
  };

  function requireValue(condition, message) {
    if (!condition) throw new Error(message);
  }

  function displayValue(value) {
    return value === null || value === undefined ? "—" : String(value);
  }

  function locationText(sheetName, fieldName, sourceRowNumber, targetRowNumber) {
    const sides = [];
    if (sourceRowNumber) sides.push("左侧第 " + sourceRowNumber + " 行");
    if (targetRowNumber) sides.push("右侧第 " + targetRowNumber + " 行");
    return sheetName + " · " + fieldName + (sides.length ? " · " + sides.join(" / ") : "");
  }

  function mapField(
    sheet,
    row,
    fieldName,
    sourceValue,
    targetValue,
    definition,
    status,
  ) {
    return {
      name: fieldName,
      status,
      sourceValue: displayValue(sourceValue),
      targetValue: displayValue(targetValue),
      sourceRowNumber: row.source?.row_number || null,
      targetRowNumber: row.target?.row_number || null,
      location: locationText(
        sheet.sheet_name,
        fieldName,
        row.source?.row_number,
        row.target?.row_number,
      ),
      definition: definition || null,
    };
  }

  function mapRowFields(sheet, row, definitions) {
    if (row.status === "modified") {
      return (row.changes || []).map((change) => mapField(
        sheet,
        row,
        change.field,
        change.source,
        change.target,
        definitions.get(change.field),
        change.status,
      ));
    }

    const side = row.status === "source_only" ? "source" : "target";
    return Object.entries(row[side]?.values || {}).map(([fieldName, value]) => mapField(
      sheet,
      row,
      fieldName,
      side === "source" ? value : null,
      side === "target" ? value : null,
      definitions.get(fieldName),
      row.status,
    ));
  }

  function mapSheet(sheet) {
    const definitions = new Map(
      (sheet.fields || []).map((field) => [field.name, { ...field }]),
    );
    const rowChange = {
      modified: "modified",
      source_only: "deleted",
      target_only: "added",
    };
    return {
      id: sheet.sheet_name,
      label: sheet.sheet_name,
      status: sheet.status,
      primaryKey: sheet.primary_key,
      summary: { ...sheet.summary },
      fieldDefinitions: [...definitions.values()],
      errors: [...(sheet.errors || [])],
      rows: (sheet.rows || []).map((row) => ({
        key: row.key,
        label: "主键：" + (sheet.primary_key || "未提供"),
        status: row.status,
        change: rowChange[row.status] || row.status,
        sourceRowNumber: row.source?.row_number ?? null,
        targetRowNumber: row.target?.row_number ?? null,
        sourceValues: row.source ? { ...(row.source.values || {}) } : null,
        targetValues: row.target ? { ...(row.target.values || {}) } : null,
        fields: mapRowFields(sheet, row, definitions),
      })),
    };
  }

  function errorText(errors, fallback) {
    if (!errors.length) return fallback;
    return errors.map((error) => {
      const scope = error.sheet_name ? " · " + error.sheet_name : "";
      return error.code + scope + "：" + error.message;
    }).join("；");
  }

  function mapDiffPayload(payload, candidate) {
    requireValue(payload && typeof payload === "object", "Diff 响应不是对象");
    requireValue(payload.schema_version === SCHEMA_VERSION, "Diff schema_version 不受支持");
    requireValue(
      payload.direction?.source === "left" && payload.direction?.target === "right",
      "Diff 方向必须为 source=left、target=right",
    );
    requireValue(payload.workbook && WORKBOOK_STATES[payload.workbook.status], "工作簿状态不受支持");
    requireValue(Array.isArray(payload.sheets), "Diff sheets 必须是数组");
    requireValue(Array.isArray(payload.errors), "Diff errors 必须是数组");

    const status = payload.workbook.status;
    const errors = [...payload.errors];
    return {
      candidate,
      state: WORKBOOK_STATES[status],
      partial: status === "partial",
      workbook: { ...payload.workbook },
      summary: { ...payload.summary },
      errors,
      error: errorText(
        errors,
        status === "failed" ? "工作簿差异比对失败。" : "部分 Sheet 比对失败。",
      ),
      sheets: payload.sheets.map(mapSheet),
    };
  }

  globalThis.M2DiffMapper = Object.freeze({
    mapDiffPayload,
  });
})();
