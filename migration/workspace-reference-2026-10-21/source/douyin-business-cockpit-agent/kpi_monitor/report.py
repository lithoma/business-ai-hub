"""Jinja2 report renderer for the anomaly report template.

Loads ``templates/anomaly_report.html`` and renders it with populated
context data.  The template file remains separate from this Python
business logic — this module only constructs the context dict and
calls Jinja2.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional


class ReportRenderer:
    """Renders the HTML anomaly report from a Jinja2 template.

    Parameters
    ----------
    template_path : str, optional
        Path to the Jinja2 template.  Defaults to
        ``<project_root>/templates/anomaly_report.html``.
    """

    def __init__(self, template_path: Optional[str] = None):
        if template_path is None:
            # Default: templates/anomaly_report.html relative to project root
            project_root = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
            template_path = os.path.join(project_root, "templates", "anomaly_report.html")
        self.template_path = template_path
        self._template_cache = None

    def _load_template(self) -> str:
        """Read the template file (cached after first read)."""
        if self._template_cache is None:
            with open(self.template_path, "r", encoding="utf-8") as f:
                self._template_cache = f.read()
        return self._template_cache

    def build_context(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List,
        ai_insights,
        config,
        sim_info: Optional[dict] = None,
        run_id: str = "",
    ) -> Dict:
        """Build the Jinja2 context dict from monitoring data.

        Parameters
        ----------
        monitoring_rows : list[dict]
            Output from ``KPIMonitor.detect_anomalies``.
        anomaly_results : list[AnomalyResult]
            Triggered anomaly rules.
        ai_insights : AgentInsights
            Structured insights from the Genie agent.
        config : MonitorConfig
            Provides brand, scope, data source labels.
        sim_info : dict, optional
            Simulation metadata (sim_date, comparison_date, etc.).
        run_id : str
            Job run identifier.
        """

        # Determine dates
        report_date = sim_info.get("sim_date", "") if sim_info else ""
        if not report_date and monitoring_rows:
            report_date = monitoring_rows[0].get("monitor_date", "")

        comparison_date = sim_info.get("comparison_date", "") if sim_info else ""
        if not comparison_date and monitoring_rows:
            # Use the previous observation date as comparison
            comparison_date = "上一观测日"

        # Data source label
        data_source = (
            "模拟仿真数据 (KPI Simulation)" if config.demo_mode
            else f"{config.source_view} (Metric View)"
        )

        # Build metrics table rows
        metrics = []
        for r in monitoring_rows:
            val_str = f"{r['kpi_value']:,.2f}" if r["kpi_value"] is not None else "N/A"
            prev_str = f"{r['prev_value']:,.2f}" if r["prev_value"] is not None else "N/A"
            if r["pct_change"] is not None:
                change_str = f"{r['pct_change']:+.1%}"
            else:
                change_str = "N/A"
            status = "异常" if r["is_anomaly"] else "正常"
            metrics.append({
                "name": r["kpi_display_name"],
                "current": val_str,
                "previous": prev_str,
                "change": change_str,
                "status": status,
            })

        # Executive summary
        anomaly_count = len(anomaly_results)
        if anomaly_count == 0:
            executive_summary = "本期所有指标均在正常范围内，未检测到异常。"
        else:
            critical = sum(1 for a in anomaly_results if a.severity.name == "CRITICAL")
            warning = sum(1 for a in anomaly_results if a.severity.name == "WARNING")
            parts = [f"检测到 {anomaly_count} 项异常指标"]
            if critical:
                parts.append(f"其中 {critical} 项严重异常")
            if warning:
                parts.append(f"{warning} 项警告级别异常")
            executive_summary = "，".join(parts) + "。"

        # AI insights
        if ai_insights and ai_insights.success:
            ai_data = {
                "core_finding": ai_insights.core_finding or "（无核心发现）",
                "analysis": ai_insights.analysis or "（无分析）",
                "uncertainties": ai_insights.uncertainties or "（无需验证因素）",
            }
        else:
            error_msg = ai_insights.error if ai_insights else "Agent unavailable"
            ai_data = {
                "core_finding": "AI 分析暂时不可用",
                "analysis": f"Agent 调用失败: {error_msg}",
                "uncertainties": "因 Agent 不可用，无法提供尚待验证因素。",
            }

        # Recommendations (derived from anomaly rules)
        recommendations = self._generate_recommendations(anomaly_results, config)

        # Data status
        if config.demo_mode:
            data_status = "模拟数据（非生产数据）"
        else:
            data_status = "生产数据（已验证新鲜度）"

        detection_method = (
            "确定性异常检测：绝对值边界 + 环比变化 + Z-score"
        )

        return {
            "report_date": report_date,
            "brand": config.brand,
            "business_scope": config.business_scope,
            "comparison_date": comparison_date,
            "data_source": data_source,
            "anomaly_count": anomaly_count,
            "executive_summary": executive_summary,
            "metrics": metrics,
            "ai_insights": ai_data,
            "recommendations": recommendations,
            "data_status": data_status,
            "detection_method": detection_method,
            "job_run_id": run_id,
        }

    def _generate_recommendations(self, anomaly_results, config) -> List[str]:
        """Generate action recommendations from anomaly results."""
        if not anomaly_results:
            return ["继续正常监控，无需特殊行动。"]

        recs = []
        seen_kpis = set()
        for a in anomaly_results:
            if a.kpi_name in seen_kpis:
                continue
            seen_kpis.add(a.kpi_name)
            if a.kpi_name == "gmv":
                recs.append(
                    f"检查 GMV 异常原因：确认是否有促销活动变化、"
                    f"流量波动或产品断货情况。"
                )
            elif a.kpi_name == "sales_qty":
                recs.append(
                    f"检查销量异常原因：确认商品库存状态、"
                    f"价格变动及直播间流量情况。"
                )
            elif a.kpi_name == "refund_amt":
                recs.append(
                    f"检查退款金额异常原因：分析退款商品分布、"
                    f"退款原因及物流时效问题。"
                )
            elif a.kpi_name == "refund_rate":
                recs.append(
                    f"检查退款率异常原因：对比历史退款率趋势，"
                    f"确认是否存在批次质量问题。"
                )
            elif a.kpi_name == "avg_unit_gmv":
                recs.append(
                    f"检查客单价异常原因：分析产品结构变化、"
                    f"促销力度及用户画像变化。"
                )
            else:
                recs.append(f"关注 {a.kpi_display_name} 异常变化，进一步排查原因。")

        recs.append("将异常情况同步至相关业务团队，持续跟踪指标恢复情况。")
        return recs

    def render(self, context: Dict) -> str:
        """Render the Jinja2 template with the given context."""
        from jinja2 import Template

        template_str = self._load_template()
        template = Template(template_str)
        return template.render(**context)

    def render_report(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List,
        ai_insights,
        config,
        sim_info: Optional[dict] = None,
        run_id: str = "",
    ) -> str:
        """Convenience: build context and render in one call."""
        context = self.build_context(
            monitoring_rows, anomaly_results, ai_insights,
            config, sim_info, run_id,
        )
        return self.render(context)