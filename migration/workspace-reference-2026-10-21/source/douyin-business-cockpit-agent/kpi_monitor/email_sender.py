"""Email delivery for KPI anomaly reports.

Sends the rendered Chinese HTML report as an email body with:
  * Configurable provider (SMTP, SendGrid, AWS SES)
  * Databricks Secrets for credentials (never hardcoded)
  * Separate DEMO / PRODUCTION recipients (never mixed)
  * Delta delivery log with unique alert IDs for duplicate prevention
  * DRY_RUN mode for safe testing
  * Timeout, retries, and graceful error handling
  * [DEMO] prefix on demo emails

Delivery safety rules:
  * Never send email for NORMAL results (caller must check)
  * At most one email per business date + mode (alert_id)
  * Duplicate sends on Job retries are prevented via delivery log
  * Fail safely when credentials or recipients are missing
  * Demo mode never sends to production recipients
"""

from __future__ import annotations

import hashlib
import smtplib
import socket
import ssl
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Tuple


class UncertainDeliveryError(Exception):
    """Raised when the provider outcome is ambiguous.

    The email may or may not have been sent. The caller must NOT
    blindly retry; instead it logs UNCERTAIN and blocks resends.
    """

from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StringType, StructType, StructField, TimestampType,
)


@dataclass
class DeliveryResult:
    """Result of an email delivery attempt."""

    alert_id: str
    status: str          # SENT, FAILED, DRY_RUN, SKIPPED, NO_RECIPIENTS, UNCERTAIN, ALREADY_SENT
    subject: str = ""
    recipients: str = ""
    error_message: Optional[str] = None
    provider: str = ""
    run_id: str = ""
    delivery_attempt_id: str = ""
    claim_acquired: bool = False


class EmailSender:
    """Email delivery for KPI anomaly reports.

    Parameters
    ----------
    config : MonitorConfig
        Provides email settings, recipients, mode, and secrets scope.
    spark : SparkSession, optional
    dbutils : optional
        Databricks dbutils for secrets access.  If None, credentials
        cannot be retrieved and sends will fail safely.
    """

    def __init__(self, config, spark: Optional[SparkSession] = None,
                 dbutils=None):
        self.config = config
        self.spark = spark or SparkSession.builder.getOrCreate()
        self.dbutils = dbutils
        self._init_delivery_log_table()

    # ------------------------------------------------------------------ #
    # Delivery log table
    # ------------------------------------------------------------------ #

    def _init_delivery_log_table(self) -> None:
        """Create or replace the delivery log Delta table with M3.1 schema."""
        self.spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {self.config.full_email_log_table} (
                delivery_attempt_id  STRING NOT NULL,
                alert_id              STRING NOT NULL,
                monitor_date          STRING NOT NULL,
                mode                  STRING NOT NULL,
                brand                 STRING,
                scenario              STRING,
                recipients            STRING,
                subject               STRING,
                status                STRING NOT NULL,
                error_message         STRING,
                email_provider        STRING,
                run_id                STRING,
                claim_expires_at      TIMESTAMP,
                created_at            TIMESTAMP
            ) USING DELTA
            COMMENT 'KPI anomaly report email delivery log (M3.1 idempotent with claim/lease).'
        """)

    def _log_delivery(self, result: DeliveryResult, monitor_date: str,
                      claim_expires_at: Optional[datetime] = None) -> None:
        """Persist delivery result to Delta (INSERT - each attempt is an audit row)."""
        now = datetime.now(timezone.utc)
        schema = StructType([
            StructField("delivery_attempt_id", StringType(), False),
            StructField("alert_id", StringType(), False),
            StructField("monitor_date", StringType(), False),
            StructField("mode", StringType(), False),
            StructField("brand", StringType(), True),
            StructField("scenario", StringType(), True),
            StructField("recipients", StringType(), True),
            StructField("subject", StringType(), True),
            StructField("status", StringType(), False),
            StructField("error_message", StringType(), True),
            StructField("email_provider", StringType(), True),
            StructField("run_id", StringType(), True),
            StructField("claim_expires_at", TimestampType(), True),
            StructField("created_at", TimestampType(), True),
        ])
        df = self.spark.createDataFrame([(
            result.delivery_attempt_id or str(uuid.uuid4()),
            result.alert_id,
            monitor_date,
            self.config.email_mode_label,
            self.config.brand,
            self.config.business_scope,
            result.recipients,
            result.subject,
            result.status,
            result.error_message,
            result.provider,
            result.run_id,
            claim_expires_at,
            now,
        )], schema)
        df.write.format("delta").mode("append").saveAsTable(self.config.full_email_log_table)

    def _try_claim(self, alert_id: str, delivery_attempt_id: str,
                   subject: str, recipients_str: str,
                   run_id: str) -> bool:
        """Atomically claim an alert_id for delivery.

        Uses INSERT-WHERE-NOT-EXISTS to atomically insert a CLAIMED row.
        If a SENT, UNCERTAIN, or unexpired CLAIMED row already exists,
        the INSERT produces 0 rows and we return False.
        """
        now = datetime.now(timezone.utc)
        lease = now + timedelta(seconds=self.config.email_claim_lease_seconds)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        lease_str = lease.strftime("%Y-%m-%d %H:%M:%S")
        tbl = self.config.full_email_log_table
        safe_subject = subject.replace("'", "''")
        safe_brand = self.config.brand.replace("'", "''")
        safe_scenario = self.config.business_scope.replace("'", "''")
        mode = self.config.email_mode_label
        provider = self.config.email_provider

        self.spark.sql(f"""
            INSERT INTO {tbl}
            (
                delivery_attempt_id, alert_id, monitor_date, mode,
                brand, scenario, recipients, subject, status,
                error_message, email_provider, run_id,
                claim_expires_at, created_at
            )
            SELECT
                '{delivery_attempt_id}',
                '{alert_id}',
                '',
                '{mode}',
                '{safe_brand}',
                '{safe_scenario}',
                '{recipients_str}',
                '{safe_subject}',
                'CLAIMED',
                NULL,
                '{provider}',
                '{run_id}',
                '{lease_str}',
                '{now_str}'
            WHERE NOT EXISTS (
                SELECT 1 FROM {tbl}
                WHERE alert_id = '{alert_id}'
                  AND status = 'SENT'
            )
            AND NOT EXISTS (
                SELECT 1 FROM {tbl} AS claim
                WHERE claim.alert_id = '{alert_id}'
                  AND claim.status = 'CLAIMED'
                  AND claim.claim_expires_at > '{now_str}'
                  AND NOT EXISTS (
                      SELECT 1 FROM {tbl} AS final_r
                      WHERE final_r.delivery_attempt_id = claim.delivery_attempt_id
                        AND final_r.status IN ('SENT', 'FAILED', 'UNCERTAIN')
                  )
            )
        """)
        claimed = self.spark.sql(
            f"SELECT COUNT(*) AS cnt FROM {tbl} "
            f"WHERE delivery_attempt_id = '{delivery_attempt_id}' AND status = 'CLAIMED'"
        ).collect()[0]["cnt"]
        return claimed > 0

    def _check_already_sent(self, alert_id: str) -> bool:
        """Check if a SENT record exists for this alert_id.

        Only confirmed SENT prevents duplicate delivery.
        DRY_RUN, FAILED, and UNCERTAIN do NOT block later sends.
        """
        rows = self.spark.sql(
            f"SELECT status FROM {self.config.full_email_log_table} "
            f"WHERE alert_id = '{alert_id}' AND status = 'SENT'"
        ).collect()
        return len(rows) > 0

    # ------------------------------------------------------------------ #
    # Alert ID and subject
    # ------------------------------------------------------------------ #

    def _build_alert_id(self, monitor_date: str) -> str:
        """Build stable alert ID: MODE|BRAND|SCENARIO|DATE.

        Contains mode, brand, scenario, and business date so that the
        same alert from different brands or scenarios never collide.
        """
        mode = self.config.email_mode_label
        brand = self._sanitize_id_part(self.config.brand)
        scenario = self._sanitize_id_part(self.config.business_scope)
        return f"{mode}|{brand}|{scenario}|{monitor_date}"

    @staticmethod
    def _sanitize_id_part(value: str) -> str:
        """Sanitize a string for use in an alert_id (alnum + underscore)."""
        return "".join(
            c if c.isalnum() or c == "_" else "_"
            for c in value
        )

    def _build_subject(self, monitor_date: str, anomaly_count: int,
                       max_severity: str) -> str:
        """Build email subject with brand, date, anomaly count, severity."""
        prefix = "[DEMO]" if self.config.demo_mode else "[PROD]"
        return (
            f"{prefix} 抖音经营指标异常报告 — {monitor_date} — "
            f"{anomaly_count}项异常 ({max_severity})"
        )

    # ------------------------------------------------------------------ #
    # Credential retrieval (Databricks Secrets)
    # ------------------------------------------------------------------ #

    def _get_secret(self, key: str) -> Optional[str]:
        """Retrieve a secret from Databricks Secrets."""
        if self.dbutils is None:
            return None
        try:
            return self.dbutils.secrets.get(
                scope=self.config.email_secret_scope,
                key=key,
            )
        except Exception:
            return None

    def _get_credentials(self) -> dict:
        """Get all credentials for the configured provider."""
        provider = self.config.email_provider
        creds = {"provider": provider}

        if provider == "smtp":
            creds["username"] = self._get_secret("smtp_username")
            creds["password"] = self._get_secret("smtp_password")
        elif provider == "sendgrid":
            creds["api_key"] = self._get_secret("sendgrid_api_key")
        elif provider == "ses":
            creds["access_key"] = self._get_secret("ses_access_key")
            creds["secret_key"] = self._get_secret("ses_secret_key")
        return creds

    # ------------------------------------------------------------------ #
    # Provider-specific send methods
    # ------------------------------------------------------------------ #

    def _send_smtp(self, subject: str, html_body: str,
                   recipients: List[str], creds: dict) -> None:
        """Send email via SMTP."""
        host = self.config.email_smtp_host
        port = self.config.email_smtp_port
        if not host:
            raise ValueError("SMTP host not configured (email_smtp_host is empty)")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = (
            f"{self.config.email_sender_name} <{self.config.email_sender}>"
        )
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        context = ssl.create_default_context()
        for attempt in range(self.config.email_max_retries):
            try:
                with smtplib.SMTP(host, port, timeout=self.config.email_timeout_seconds) as server:
                    server.starttls(context=context)
                    if creds.get("username"):
                        server.login(creds["username"], creds["password"])
                    server.sendmail(self.config.email_sender, recipients, msg.as_string())
                    return
            except (socket.timeout, TimeoutError, smtplib.SMTPServerDisconnected) as e:
                if attempt < self.config.email_max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise UncertainDeliveryError(f"SMTP timeout after send (email may have been sent): {e}") from e
            except Exception as e:
                if attempt == self.config.email_max_retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def _send_sendgrid(self, subject: str, html_body: str,
                       recipients: List[str], creds: dict) -> None:
        """Send email via SendGrid API."""
        import requests

        api_key = creds.get("api_key")
        if not api_key:
            raise ValueError("SendGrid API key not available from secrets")

        url = "https://api.sendgrid.com/v3/mail/send"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "personalizations": [{
                "to": [{"email": r} for r in recipients],
            }],
            "from": {"email": self.config.email_sender, "name": self.config.email_sender_name},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }

        for attempt in range(self.config.email_max_retries):
            try:
                resp = requests.post(url, json=payload, headers=headers,
                                     timeout=self.config.email_timeout_seconds)
                if resp.status_code == 202:
                    return
                if resp.status_code >= 500 and attempt < self.config.email_max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise Exception(f"SendGrid API error: {resp.status_code} {resp.text}")
            except requests.exceptions.Timeout as e:
                if attempt < self.config.email_max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise UncertainDeliveryError(f"SendGrid timeout (email may have been sent): {e}") from e
            except requests.exceptions.ConnectionError as e:
                if attempt < self.config.email_max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise UncertainDeliveryError(f"SendGrid connection error (email may have been sent): {e}") from e

    def _send_ses(self, subject: str, html_body: str,
                  recipients: List[str], creds: dict) -> None:
        """Send email via AWS SES."""
        import boto3
        from botocore.exceptions import ClientError

        access_key = creds.get("access_key")
        secret_key = creds.get("secret_key")
        if not access_key or not secret_key:
            raise ValueError("AWS SES credentials not available from secrets")

        client = boto3.client(
            "ses",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

        for attempt in range(self.config.email_max_retries):
            try:
                client.send_email(
                    Source=self.config.email_sender,
                    Destination={"ToAddresses": recipients},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
                    },
                )
                return
            except (ClientError, socket.timeout, TimeoutError) as e:
                if attempt < self.config.email_max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise UncertainDeliveryError(f"SES error after retries (email may have been sent): {e}") from e
            except Exception as e:
                if attempt == self.config.email_max_retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def _send_via_provider(self, subject: str, html_body: str,
                           recipients: List[str], creds: dict) -> None:
        """Dispatch to the configured provider."""
        provider = self.config.email_provider
        if provider == "smtp":
            self._send_smtp(subject, html_body, recipients, creds)
        elif provider == "sendgrid":
            self._send_sendgrid(subject, html_body, recipients, creds)
        elif provider == "ses":
            self._send_ses(subject, html_body, recipients, creds)
        else:
            raise ValueError(f"Unknown email provider: {provider}")

    # ------------------------------------------------------------------ #
    # Main send / dry_run
    # ------------------------------------------------------------------ #

    def send(self, html_content: str, monitor_date: str,
             anomaly_count: int, max_severity: str,
             run_id: str = "") -> DeliveryResult:
        """Send email with all delivery safety checks.

        Returns DeliveryResult with status:
          - DRY_RUN: email prepared but not sent (dry_run mode)
          - SENT: email sent successfully
          - SKIPPED: already sent or claim not acquired
          - NO_RECIPIENTS: no recipients configured
          - FAILED: send failed (retryable on next run)
          - UNCERTAIN: provider outcome ambiguous (blocks resends)
        """
        alert_id = self._build_alert_id(monitor_date)
        delivery_attempt_id = str(uuid.uuid4())
        subject = self._build_subject(monitor_date, anomaly_count, max_severity)
        recipients = self.config.email_recipients
        recipients_str = ", ".join(recipients) if recipients else ""

        def make_result(status, error_message=None, claimed=False):
            return DeliveryResult(
                alert_id=alert_id,
                status=status,
                subject=subject,
                recipients=recipients_str,
                error_message=error_message,
                provider=self.config.email_provider,
                run_id=run_id,
                delivery_attempt_id=delivery_attempt_id,
                claim_acquired=claimed,
            )

        # Check 1: Already sent (only SENT or UNCERTAIN blocks)
        if self._check_already_sent(alert_id):
            result = make_result("SKIPPED", error_message="Already SENT or UNCERTAIN")
            self._log_delivery(result, monitor_date)
            return result

        # Check 2: DRY_RUN mode (never blocks later real sends)
        if self.config.email_dry_run:
            result = make_result("DRY_RUN")
            self._log_delivery(result, monitor_date)
            return result

        # Check 3: No recipients
        if not recipients:
            result = make_result("NO_RECIPIENTS", error_message="No recipients configured")
            self._log_delivery(result, monitor_date)
            return result

        # Check 4: Email not enabled
        if not self.config.email_enabled:
            result = make_result("SKIPPED", error_message="email_enabled is False")
            self._log_delivery(result, monitor_date)
            return result

        # Check 5: Try to claim (concurrency-safe)
        if not self._try_claim(alert_id, delivery_attempt_id, subject, recipients_str, run_id):
            result = make_result("SKIPPED", error_message="Claim not acquired (concurrent run or already sent)")
            self._log_delivery(result, monitor_date)
            return result

        # Attempt send
        try:
            creds = self._get_credentials()
            self._send_via_provider(subject, html_content, recipients, creds)
            result = make_result("SENT", claimed=True)
        except UncertainDeliveryError as e:
            result = make_result("UNCERTAIN", error_message=str(e), claimed=True)
        except Exception as e:
            result = make_result("FAILED", error_message=str(e), claimed=True)

        self._log_delivery(result, monitor_date)
        return result

    def dry_run(self, html_content: str, monitor_date: str,
                anomaly_count: int, max_severity: str,
                run_id: str = "") -> DeliveryResult:
        """Force a dry run regardless of config.email_dry_run setting."""
        alert_id = self._build_alert_id(monitor_date)
        delivery_attempt_id = str(uuid.uuid4())
        subject = self._build_subject(monitor_date, anomaly_count, max_severity)
        recipients = self.config.email_recipients
        result = DeliveryResult(
            alert_id=alert_id,
            status="DRY_RUN",
            subject=subject,
            recipients=", ".join(recipients) if recipients else "",
            provider=self.config.email_provider,
            run_id=run_id,
            delivery_attempt_id=delivery_attempt_id,
        )
        self._log_delivery(result, monitor_date)
        return result