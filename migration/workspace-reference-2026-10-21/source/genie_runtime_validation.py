# Databricks notebook source
# DBTITLE 1,Genie Runtime Validation
# ── Genie A1 Runtime Validation ──────────────────────────────
# Lightweight validation: verifies SDK version, w.genie availability,
# and one live Genie A1 call with simulated KPI context.
# No simulation reset, no reports, no emails, no alerts.

import sys, re, json, time
from datetime import datetime, timezone

print("=" * 60)
print("Genie A1 Runtime Validation")
print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
print("=" * 60)

# ── 1. Check databricks-sdk version ──
print("\n[1] Checking databricks-sdk version...")
try:
    import databricks.sdk
    sdk_version = getattr(databricks.sdk, "__version__", "unknown")
    print(f"  SDK version: {sdk_version}")
    parts = sdk_version.split(".")
    if len(parts) >= 3:
        major, minor = int(parts[0]), int(parts[1])
        if major > 0 or (major == 0 and minor >= 122):
            print(f"  ✓ Version >= 0.122.0 (required for start_conversation_and_wait)")
        else:
            print(f"  ✗ Version < 0.122.0 — start_conversation_and_wait may not be available")
    else:
        print(f"  ? Could not parse version string")
except ImportError as e:
    print(f"  ✗ databricks-sdk not importable: {e}")
    dbutils.jobs.taskValues.set("genie_validation_status", "FAILED")
    dbutils.notebook.exit("FAILED: SDK not importable")

# ── 2. Create WorkspaceClient and check w.genie ──
print("\n[2] Creating WorkspaceClient...")
try:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    print(f"  ✓ WorkspaceClient created")
except Exception as e:
    print(f"  ✗ WorkspaceClient creation failed: {e}")
    dbutils.jobs.taskValues.set("genie_validation_status", "FAILED")
    dbutils.notebook.exit(f"FAILED: {e}")

print("\n[3] Checking w.genie availability...")
has_genie = hasattr(w, "genie")
print(f"  w.genie available: {has_genie}")
if has_genie:
    print(f"  ✓ w.genie exists — can use start_conversation_and_wait")
else:
    print(f"  ✗ w.genie NOT available — will try REST API fallback")

# ── 4. Build simulated KPI context prompt ──
GENIE_SPACE_ID = "01f1c60941551d9b94f73515f09e4b7b"

prompt = """请分析以下经营指标异常监测结果，并提供中文商业洞察。

监测日期：2025-01-14
品牌：Haleon
监测范围：抖音电商整体

【关键经营指标】
  - GMV（商品交易总额）: 本期=8,250,000.00, 对比期=9,800,000.00, Z值=-2.85, 状态=异常
  - 平均客单价: 本期=125.50, 对比期=140.20, Z值=-2.31, 状态=异常
  - 退款金额: 本期=450,000.00, 对比期=280,000.00, Z值=+3.12, 状态=异常
  - 直播间转化率: 本期=2.15%, 对比期=3.80%, Z值=-2.67, 状态=异常
  - 订单量: 本期=65,740, 对比期=69,900, Z值=-1.45, 状态=正常

【异常详情】
  - [CRITICAL] GMV（商品交易总额） | z_score | Z值-2.85低于阈值-2.5，GMV显著下降
  - [CRITICAL] 退款金额 | z_score | Z值+3.12高于阈值+2.5，退款金额异常上升
  - [WARNING] 平均客单价 | z_score | Z值-2.31低于阈值-2.0，客单价下降
  - [WARNING] 直播间转化率 | z_score | Z值-2.67低于阈值-2.5，转化率下降

【重要提示】以下数据来自模拟仿真环境，并非真实业务数据。请勿查询您的数据源来验证这些数据。所有分析必须仅基于下方提供的结构化数据。

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

print("\n[4] Built simulated KPI context prompt")
print(f"  Space ID: {GENIE_SPACE_ID}")
print(f"  Prompt length: {len(prompt)} chars")

# ── 5. Make ONE live Genie A1 call ──
print("\n[5] Calling A1 Genie Space (live, ONE request only)...")
response_text = ""
error_msg = None
time_start = time.time()

if has_genie:
    try:
        print("  Using SDK: w.genie.start_conversation_and_wait")
        msg = w.genie.start_conversation_and_wait(
            space_id=GENIE_SPACE_ID,
            content=prompt,
        )
        elapsed = time.time() - time_start
        print(f"  Genie call completed in {elapsed:.1f}s")
        
        attachments = getattr(msg, "attachments", None) or []
        for att in attachments:
            text_obj = getattr(att, "text", None)
            if text_obj is None:
                continue
            if hasattr(text_obj, "content"):
                response_text += text_obj.content or ""
            elif isinstance(text_obj, str):
                response_text += text_obj
        
        if not response_text:
            for field in ("content", "answer"):
                val = getattr(msg, field, None)
                if val and isinstance(val, str):
                    response_text = val
                    break
    except Exception as e:
        elapsed = time.time() - time_start
        print(f"  SDK call failed after {elapsed:.1f}s: {type(e).__name__}: {e}")
        error_msg = f"SDK: {type(e).__name__}: {e}"
        
        print("  Falling back to REST API...")
        try:
            resp = w.api_client.do(
                method="POST",
                path=f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/start-conversation",
                body={"content": prompt},
                headers={"Content-Type": "application/json"},
            )
            conversation_id = resp.get("conversation_id")
            message_id = resp.get("message_id")
            if conversation_id and message_id:
                for _ in range(60):
                    msg = w.api_client.do(
                        method="GET",
                        path=f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/conversations/{conversation_id}/messages/{message_id}",
                    )
                    status = msg.get("status", "")
                    if isinstance(status, dict):
                        status = status.get("value", str(status))
                    if str(status).upper() in ("COMPLETED", "FAILED", "ERROR"):
                        break
                    time.sleep(5)
                
                for att in msg.get("attachments", []) or []:
                    if not isinstance(att, dict):
                        continue
                    text_obj = att.get("text")
                    if text_obj is None:
                        continue
                    if isinstance(text_obj, dict):
                        response_text += text_obj.get("content", "") or ""
                    elif isinstance(text_obj, str):
                        response_text += text_obj
            else:
                error_msg = "REST: no conversation/message ID"
        except Exception as e2:
            error_msg = f"REST fallback also failed: {e2}"
else:
    print("  Using REST API (no w.genie available)")
    try:
        resp = w.api_client.do(
            method="POST",
            path=f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/start-conversation",
            body={"content": prompt},
            headers={"Content-Type": "application/json"},
        )
        conversation_id = resp.get("conversation_id")
        message_id = resp.get("message_id")
        if not (conversation_id and message_id):
            error_msg = "REST: no conversation/message ID"
        else:
            for _ in range(60):
                msg = w.api_client.do(
                    method="GET",
                    path=f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/conversations/{conversation_id}/messages/{message_id}",
                )
                status = msg.get("status", "")
                if isinstance(status, dict):
                    status = status.get("value", str(status))
                if str(status).upper() in ("COMPLETED", "FAILED", "ERROR"):
                    break
                time.sleep(5)
            
            for att in msg.get("attachments", []) or []:
                if not isinstance(att, dict):
                    continue
                text_obj = att.get("text")
                if text_obj is None:
                    continue
                if isinstance(text_obj, dict):
                    response_text += text_obj.get("content", "") or ""
                elif isinstance(text_obj, str):
                    response_text += text_obj
    except Exception as e:
        error_msg = f"REST: {type(e).__name__}: {e}"

elapsed_total = time.time() - time_start

# ── 6. Verify text extraction ──
print("\n[6] Verifying response text extraction...")
if response_text:
    print(f"  ✓ Response text received ({len(response_text)} chars)")
    print(f"  Elapsed: {elapsed_total:.1f}s")
    
    def extract_section(text, header):
        pattern = rf"{header}\s*\n(.*?)(?=##|$)"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else ""
    
    core = extract_section(response_text, "## 核心发现")
    analysis = extract_section(response_text, "## 数据支持的分析")
    uncertainties = extract_section(response_text, "## 尚待验证的因素")
    
    print(f"\n  ── Extracted Sections ──")
    print(f"  核心发现 ({len(core)} chars):")
    print(f"    {core[:200]}{'...' if len(core) > 200 else ''}")
    print(f"  数据支持的分析 ({len(analysis)} chars):")
    print(f"    {analysis[:200]}{'...' if len(analysis) > 200 else ''}")
    print(f"  尚待验证的因素 ({len(uncertainties)} chars):")
    print(f"    {uncertainties[:200]}{'...' if len(uncertainties) > 200 else ''}")
    
    has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in response_text)
    print(f"\n  Chinese text detected: {has_chinese}")
    print(f"  All 3 sections found: {bool(core and analysis and uncertainties)}")
    
    keywords = ["GMV", "退款", "客单价", "转化率"]
    found_kw = [kw for kw in keywords if kw in response_text]
    print(f"  Context keywords found: {found_kw}")
    
    print(f"\n  ── Full Response ──")
    print(response_text)
    
    dbutils.jobs.taskValues.set("genie_validation_status", "SUCCESS")
    dbutils.jobs.taskValues.set("genie_response_chars", len(response_text))
    dbutils.jobs.taskValues.set("genie_has_chinese", has_chinese)
    dbutils.jobs.taskValues.set("genie_sections_found", bool(core and analysis and uncertainties))
    dbutils.notebook.exit(json.dumps({
        "status": "SUCCESS",
        "sdk_version": sdk_version,
        "has_genie": has_genie,
        "response_chars": len(response_text),
        "has_chinese": has_chinese,
        "sections_found": bool(core and analysis and uncertainties),
        "elapsed_seconds": round(elapsed_total, 1),
        "keywords_found": found_kw,
    }))
else:
    print(f"  ✗ No response text received")
    print(f"  Error: {error_msg}")
    dbutils.jobs.taskValues.set("genie_validation_status", "FAILED")
    dbutils.jobs.taskValues.set("genie_validation_error", error_msg or "No response")
    dbutils.notebook.exit(json.dumps({
        "status": "FAILED",
        "sdk_version": sdk_version,
        "has_genie": has_genie,
        "error": error_msg,
    }))

# COMMAND ----------

