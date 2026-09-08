(function bootstrapReaderFormatters() {
  const { escapeHtml, formatApiErrorDetail } = window.ReaderCore;

  function safeUrl(value) {
    if (!value) return "";
    try {
      const url = new URL(value);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function localDate(value) {
    if (!value) return "暂无";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
  }

  function scopeLabel(value) {
    if (!value) return "尚未登记研究范围";
    if (typeof value === "string") return value;
    const labels = { country_iso3: "国家", seed_role: "准备状态", topic_slugs: "主题" };
    const values = { mvp_structure_only: "MVP 结构已建立，等待关联真实材料" };
    const publicEntries = Object.entries(value).filter(([key]) => labels[key]);
    return publicEntries.length
      ? publicEntries.map(([key, item]) => `${labels[key]}：${values[item] || (Array.isArray(item) ? item.join("、") : item)}`).join(" · ")
      : "已登记研究范围";
  }

  function publishedLabel(value, precision) {
    if (!value) return precision === "unknown" ? "来源未声明（不以采集时间代替）" : "暂无";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    if (precision === "year") return `${date.getUTCFullYear()} 年（年份精度）`;
    if (precision === "month") {
      const month = String(date.getUTCMonth() + 1).padStart(2, "0");
      return `${date.getUTCFullYear()} 年 ${month} 月（月份精度）`;
    }
    return localDate(value);
  }

  function formatLocator(value) {
    if (!value) return "未记录";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(formatLocator).filter(Boolean).join("；");
    if (typeof value !== "object") return String(value);
    const labels = {
      locator_type: "类型", paragraph: "段落", paragraphs: "段落", page: "页码", pages: "页码",
      section: "章节", annex: "附件", lines: "行", published_at: "发布日期",
      document_version_id: "文档版本", reviewed_by: "复核记录",
    };
    return Object.entries(value)
      .map(([key, item]) => `${labels[key] || key}：${formatLocator(item)}`)
      .join(" · ");
  }

  function formatNumber(value, unit) {
    if (value === null || value === undefined) return "暂无";
    const number = Number(value);
    if (!Number.isFinite(number)) return `${value} ${unit || ""}`.trim();
    const absolute = Math.abs(number);
    let text = number.toLocaleString("zh-CN", { maximumFractionDigits: absolute < 10 ? 2 : 1 });
    if (absolute >= 1e9) text = `${(number / 1e9).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 十亿`;
    if (absolute >= 1e12) text = `${(number / 1e12).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 万亿`;
    const unitLabels = {
      person: "人", USD: "美元", percent: "%", percent_of_GDP: "%（占 GDP）", percent_of_gdp: "%（占 GDP）",
      million_USD: "百万美元",
    };
    return `${text}${unit ? ` ${unitLabels[unit] || unit}` : ""}`;
  }

  function emptyState(message = "这项还没有入库证据。") {
    return `<div class="empty-state">${escapeHtml(message)}</div>`;
  }

  function errorState(message, retry = false) {
    return `<div class="error-state"><strong>读取失败</strong><span>${escapeHtml(formatApiErrorDetail(message))}</span>${retry ? `<button type="button" class="ghost-btn compact" data-retry-route>重新加载</button>` : ""}</div>`;
  }

  function detailSkeleton(message) {
    return `<div class="detail-skeleton" aria-live="polite"><span>${escapeHtml(message)}</span><i></i><i></i><i></i></div>`;
  }

  function sourceActionHtml(item) {
    const canonical = safeUrl(item.canonical_url);
    const discovery = safeUrl(item.discovery_url);
    const url = canonical || discovery || safeUrl(item.source_url);
    if (!url) return "";
    const label = canonical ? "打开正式来源 ↗" : discovery && !canonical ? "打开发现页 ↗" : "打开来源 ↗";
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  }

  window.ReaderFormatters = Object.freeze({
    safeUrl,
    localDate,
    scopeLabel,
    publishedLabel,
    formatLocator,
    formatNumber,
    emptyState,
    errorState,
    detailSkeleton,
    sourceActionHtml,
  });
}());
