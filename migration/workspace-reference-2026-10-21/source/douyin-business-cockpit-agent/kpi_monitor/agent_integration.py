"""A1 Genie Agent integration for anomaly analysis.

When anomalies are detected, this module triggers the existing A1 Genie
Space with a structured prompt containing the KPI values, dates, historical
comparisons, and anomaly severity.  The agent returns Chinese business
insights that are separated into verified findings and unverified
hypotheses.

Key rules:
  * **No call when NORMAL** — if no anomalies are detected, Genie is
    never invoked.
  * **Demo isolation** — in demo mode, the prompt provides all data
    directly as structured simulation context and explicitly instructs
    the agent NOT to query its own data sources for the simulated date.
  * **Graceful failure** — if the Genie API call fails, the result is
    marked as unsuccessful and a fallback message is returned.
  * **No invented data** — the agent is instructed to rely only on the
    provided context and never fabricate supporting evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class AgentInsights:
    """Structured result from the A1 Genie agent."""

    core_finding: str = ""
    analysis: str = ""
    uncertainties: str = ""
    success: bool = True
    error: Optional[str] = None


class AgentIntegration:
    """Wraps A1 Genie Space calls for anomaly analysis.

    Parameters
    ----------
    genie_space_id : str
        The A1 Genie Space ID.
    config : MonitorConfig
        Provides demo_mode and brand/scope context.
    workspace_client : optional
        A databricks.sdk WorkspaceClient.  If None, one is created.
    """

    A1_SPACE_ID = "01f1c60941551d9b94f73515f09e4b7b"

    def __init__(self, config, genie_space_id: Optional[str] = None,
                 workspace_client=None):
        self.config = config
        self.genie_space_id = genie_space_id or self.A1_SPACE_ID
        if workspace_client is not None:
            self.w = workspace_client
        else:
            try:
                from databricks.sdk import WorkspaceClient
                self.w = WorkspaceClient()
            except Exception:
                self.w = None

    # ------------------------------------------------------------------ #
    # Context formatting
    # ------------------------------------------------------------------ #

    def format_context(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List,
        sim_info: Optional[Dict] = None,
    ) -> Dict:
        """Build a structured context dict for the Genie prompt."""

        # KPI summary
        kpi_lines = []
        for r in monitoring_rows:
            val = f"{r['kpi_value']:,.2f}" if r["kpi_value"] is not None else "N/A"
            prev = f"{r['prev_value']:,.2f}" if r["prev_value"] is not None else "N/A"
            z = f"{r['z_score']:+.2f}" if r["z_score"] is not None else "N/A"
            status = "异常" if r["is_anomaly"] else "正常"
            kpi_lines.append(
                f"  - {r['kpi_display_name']}: 本期={val}, 对比期={prev}, "
                f"Z值={z}, 状态={status}"
            )

        # Anomaly details
        anomaly_lines = []
        for a in anomaly_results:
            anomaly_lines.append(
                f"  - [{a.severity.name}] {a.kpi_display_name} | "
                f"{a.rule_name} | {a.description}"
            )

        context = {
            "report_date": sim_info.get("sim_date", "") if sim_info else "",
            "brand": self.config.brand,
            "business_scope": self.config.business_scope,
            "kpi_summary": "\n".join(kpi_lines) if kpi_lines else "  (无数据)",
            "anomaly_details": "\n".join(anomaly_lines) if anomaly_lines else "  (无异常)",
            "is_demo": self.config.demo_mode,
        }
        return context

    def build_prompt(self, context: Dict) -> str:
        """Build a Chinese prompt for the Genie agent.

        In demo mode, the prompt explicitly provides all data and
        instructs the agent NOT to query its own data sources.
        """

        demo_warning = ""
        if context["is_demo"]:
            demo_warning = (
                "\n\n【重要提示】以下数据来自模拟仿真环境，并非真实业务数据。"
                "请勿查询您的数据源来验证这些数据。所有分析必须仅基于"
                "下方提供的结构化数据。"
            )

        prompt = f"""请分析以下经营指标异常监测结果，并提供中文商业洞察。

监测日期：{context['report_date']}
品牌：{context['brand']}
监测范围：{context['business_scope']}

【关键经营指标】
{context['kpi_summary']}

【异常详情】
{context['anomaly_details']}{demo_warning}

请按以下格式回复：

## 核心发现
（基于上述数据的确切发现，不超过3句话）

## 数据支持的分析
（基于上述数据的分析，包含可能的原因和影响）

## 尚待验证的因素
（需要进一步验证的假设，明确标注为未验证）

请遵守以下规则：
1. 仅基于上方提供的数据进行分析
2. 不得编造未提供的数据或证据
3. 明确区分已验证的发现和未验证的假设
4. 使用中文回复
"""
        return prompt

    # ------------------------------------------------------------------ #
    # Genie API call
    # ------------------------------------------------------------------ #

    def analyze_anomaly(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List,
        sim_info: Optional[Dict] = None,
    ) -> AgentInsights:
        """Call the A1 Genie agent with anomaly context.

        Returns AgentInsights.  If the API call fails, returns a
        failed AgentInsights with an error message.

        When config.mock_agent is True, returns a pre-defined mock
        response without calling the live Genie API (for development).
        """
        # Mock mode: skip live Genie call (development/testing)
        if getattr(self.config, 'mock_agent', False):
            return AgentInsights(
                core_finding="[MOCK] 模拟分析结果：检测到指标异常波动。",
                analysis="[MOCK] 基于模拟数据的初步分析，退款金额显著上升，超过正常阈值。",
                uncertainties="[MOCK] 需进一步验证具体退款原因和商品明细。",
                success=True,
                error=None,
            )

        context = self.format_context(monitoring_rows, anomaly_results, sim_info)
        prompt = self.build_prompt(context)

        if self.w is None:
            return AgentInsights(
                success=False,
                error="WorkspaceClient not available",
                core_finding="",
                analysis="",
                uncertainties="",
            )

        try:
            return self._call_genie(prompt)
        except Exception as e:
            return AgentInsights(
                success=False,
                error=str(e),
                core_finding="",
                analysis="",
                uncertainties="",
            )

    def _call_genie(self, prompt: str) -> AgentInsights:
        """Send prompt to A1 Genie Space and parse the response.

        Primary:  SDK ``start_conversation_and_wait`` (requires databricks-sdk >= 0.122.0).
        Fallback: REST API via ``api_client.do()`` for older SDK versions.
        """
        # Try SDK approach first (databricks-sdk >= 0.122.0)
        if hasattr(self.w, 'genie'):
            try:
                return self._call_genie_sdk(prompt)
            except Exception as e:
                # Don't leak raw SDK errors; provide a clean message
                print(f"  [Genie SDK error] {type(e).__name__}: {e}")
                # Fall through to REST API fallback

        # Fallback: REST API via api_client.do()
        return self._call_genie_rest(prompt)

    # ------------------------------------------------------------------ #
    # SDK approach
    # ------------------------------------------------------------------ #

    def _call_genie_sdk(self, prompt: str) -> AgentInsights:
        """Call Genie using ``start_conversation_and_wait`` (SDK >= 0.122.0).

        ``start_conversation_and_wait`` creates a new conversation, sends the
        message, and blocks until Genie completes — no manual polling needed.
        Returns a ``GenieMessage`` whose ``attachments`` list contains
        ``GenieAttachment`` objects with ``.text`` of type ``TextAttachment``
        (the actual text is in ``.text.content``).
        """
        msg = self.w.genie.start_conversation_and_wait(
            space_id=self.genie_space_id,
            content=prompt,
        )
        return self._extract_genie_response(msg)

    # ------------------------------------------------------------------ #
    # REST API fallback (works with any SDK version)                      #
    # ------------------------------------------------------------------ #

    def _call_genie_rest(self, prompt: str) -> AgentInsights:
        """Call Genie using the REST API via ``api_client.do()``.

        Uses ``POST /api/2.0/genie/spaces/{id}/start-conversation`` to create
        a conversation, then polls the message until completed.
        """
        import time

        # Step 1: Start a conversation (also sends the first message)
        resp = self.w.api_client.do(
            method="POST",
            path=f"/api/2.0/genie/spaces/{self.genie_space_id}/start-conversation",
            body={"content": prompt},
            headers={"Content-Type": "application/json"},
        )

        conversation_id = resp.get("conversation_id")
        message_id = resp.get("message_id")

        if not conversation_id or not message_id:
            return AgentInsights(
                success=False,
                error="Genie REST API returned no conversation/message ID",
            )

        # Step 2: Poll for message completion (max 5 minutes)
        msg = None
        for _ in range(60):
            msg = self.w.api_client.do(
                method="GET",
                path=(
                    f"/api/2.0/genie/spaces/{self.genie_space_id}"
                    f"/conversations/{conversation_id}/messages/{message_id}"
                ),
            )
            status = msg.get("status", "")
            if isinstance(status, dict):
                status = status.get("value", str(status))
            if str(status).upper() in ("COMPLETED", "FAILED", "ERROR"):
                break
            time.sleep(5)

        if msg is None:
            return AgentInsights(
                success=False,
                error="Genie REST API: polling timed out",
            )

        return self._extract_genie_response_dict(msg)

    # ------------------------------------------------------------------ #
    # Response extraction
    # ------------------------------------------------------------------ #

    def _extract_genie_response(self, msg) -> AgentInsights:
        """Extract response text from a ``GenieMessage`` SDK object.

        ``msg.attachments`` is a list of ``GenieAttachment``. Each attachment
        may have a ``.text`` field which is a ``TextAttachment`` object — the
        actual text is in ``.text.content`` (not ``.text`` itself).
        """
        response_text = ""
        attachments = getattr(msg, "attachments", None) or []
        for att in attachments:
            text_obj = getattr(att, "text", None)
            if text_obj is None:
                continue
            # text_obj is a TextAttachment; the string is in .content
            if hasattr(text_obj, "content"):
                response_text += text_obj.content or ""
            elif isinstance(text_obj, str):
                response_text += text_obj

        if not response_text:
            # Fallback: check msg-level fields
            for field in ("content", "answer"):
                val = getattr(msg, field, None)
                if val and isinstance(val, str):
                    response_text = val
                    break

        if not response_text:
            return AgentInsights(
                success=False,
                error="Genie returned an empty response",
            )

        return self._parse_response(response_text)

    def _extract_genie_response_dict(self, msg: dict) -> AgentInsights:
        """Extract response text from a Genie REST API response dict."""
        response_text = ""
        attachments = msg.get("attachments", []) or []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            text_obj = att.get("text")
            if text_obj is None:
                continue
            if isinstance(text_obj, dict):
                response_text += text_obj.get("content", "") or ""
            elif isinstance(text_obj, str):
                response_text += text_obj

        if not response_text:
            for field in ("content", "answer"):
                val = msg.get(field)
                if val and isinstance(val, str):
                    response_text = val
                    break

        if not response_text:
            return AgentInsights(
                success=False,
                error="Genie returned an empty response (REST)",
            )

        return self._parse_response(response_text)

    def _parse_response(self, text: str) -> AgentInsights:
        """Parse Genie response into structured insights.

        Looks for ## 核心发现 / ## 数据支持的分析 / ## 尚待验证的因素
        headers and extracts the text under each.
        """
        import re

        def extract_section(text: str, header: str) -> str:
            pattern = rf"{header}\s*\n(.*?)(?=##|$)"
            match = re.search(pattern, text, re.DOTALL)
            return match.group(1).strip() if match else ""

        core = extract_section(text, "## 核心发现")
        analysis = extract_section(text, "## 数据支持的分析")
        uncertainties = extract_section(text, "## 尚待验证的因素")

        # Fallback: if no headers found, use the whole text as analysis
        if not core and not analysis and not uncertainties:
            analysis = text.strip()

        return AgentInsights(
            core_finding=core,
            analysis=analysis,
            uncertainties=uncertainties,
            success=True,
        )

    # ------------------------------------------------------------------ #
    # No-anomaly path
    # ------------------------------------------------------------------ #

    def normal_result(self) -> AgentInsights:
        """Return a NORMAL status result (no Genie call)."""
        return AgentInsights(
            core_finding="",
            analysis="",
            uncertainties="",
            success=True,
            error=None,
        )