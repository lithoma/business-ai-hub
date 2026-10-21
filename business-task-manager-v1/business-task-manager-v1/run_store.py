"""Durable Delta execution ledger. Requires pre-provisioned table; never creates resources."""
from __future__ import annotations
import hashlib
import uuid
from registry_tasks import table_name


def run_id(source, task_id, slot):
    return hashlib.sha256((source + '\0' + task_id + '\0' + slot).encode()).hexdigest()


class RunStore:
    def __init__(self, spark, table):
        self.spark, self.table = spark, table_name(table)

    def claim(self, task, slot, dispatch_date, config_json):
        """Existing RUNNING/FAILED/completed slots are never implicitly replayed.

        Existing Job MUST retain max_concurrent_runs=1 (Delta MERGE alone is not
        a uniqueness constraint). An interrupted RUNNING slot requires operator review.
        """
        key = run_id(task['_source'], task['task_id'], slot)
        attempt = uuid.uuid4().hex
        self.spark.sql(f'''MERGE INTO {self.table} t USING (
            SELECT :run_id run_id, :attempt attempt_id, :task_id task_id,
              :name task_name, :source source, :slot scheduled_slot,
              CAST(:dispatch_date AS DATE) dispatch_date,
              :config config_json, current_timestamp() started_at
        ) s ON t.run_id=s.run_id
        WHEN NOT MATCHED THEN INSERT
          (run_id, attempt_id, task_id, task_name, source, scheduled_slot, dispatch_date,
           status, trigger_reason, report_text, error_code, notification_status, config_json, started_at)
        VALUES (s.run_id,s.attempt_id,s.task_id,s.task_name,s.source,s.scheduled_slot,s.dispatch_date,
           'RUNNING','','','','NOT_QUEUED',s.config_json,s.started_at)''', args={
            'run_id': key, 'attempt': attempt, 'task_id': task['task_id'],
            'name': task.get('name', task['task_id']), 'source': task['_source'],
            'slot': slot, 'dispatch_date': dispatch_date.isoformat(), 'config': config_json,
        }).collect()
        rows = self.spark.sql(f'SELECT attempt_id FROM {self.table} WHERE run_id=:key', args={'key': key}).collect()
        if len(rows) != 1:
            raise RuntimeError('Run ledger uniqueness violation')
        return key if rows[0]['attempt_id'] == attempt else None

    def finish(self, key, status, reason='', report='', error=''):
        self.spark.sql(f'''UPDATE {self.table} SET status=:status, trigger_reason=:reason,
            report_text=:report, error_code=:error, finished_at=current_timestamp()
            WHERE run_id=:key AND status='RUNNING' ''', args={
                'key': key, 'status': status, 'reason': reason, 'report': report, 'error': error,
            }).collect()

    def pending_reports(self, day):
        return [row.asDict() for row in self.spark.sql(f'''SELECT run_id,task_name,report_text,dispatch_date
            FROM {self.table} WHERE dispatch_date<=CAST(:day AS DATE)
            AND status='REPORT_CREATED' AND notification_status='NOT_QUEUED'
            ORDER BY started_at,run_id''', args={'day': day.isoformat()}).collect()]

    def mark_queued(self, keys):
        if not keys:
            return
        params = {f'key{i}': key for i, key in enumerate(keys)}
        placeholders = ','.join(':' + name for name in params)
        # One Delta statement for the batch; no partial per-row queue marking.
        self.spark.sql(f"UPDATE {self.table} SET notification_status='QUEUED' WHERE run_id IN ({placeholders})",
                       args=params).collect()

    def pending_email_reports(self, day, limit=20):
        limit = max(1, min(limit, 100))
        return [row.asDict() for row in self.spark.sql(f"""SELECT run_id,task_name,config_json,report_text,trigger_reason
            FROM {self.table} WHERE dispatch_date<=CAST(:day AS DATE) AND status='REPORT_CREATED'
              AND notification_status='NOT_QUEUED'
              AND get_json_object(config_json,'$.report.delivery.enabled')='true'
            ORDER BY started_at,run_id LIMIT {limit}""", args={'day': day.isoformat()}).collect()]

    def claim_email(self, key):
        rows = self.spark.sql(f"""UPDATE {self.table} SET notification_status='EMAIL_SENDING'
            WHERE run_id=:key AND notification_status='NOT_QUEUED' AND status='REPORT_CREATED'""",
            args={'key': key}).collect()
        return bool(rows and rows[0]['num_affected_rows'] == 1)

    def email_result(self, key, status, message_id='', error='', artifacts=None):
        import json
        if status not in ('EMAIL_ACCEPTED', 'EMAIL_FAILED', 'EMAIL_UNKNOWN'):
            raise ValueError('Invalid mail outcome')
        self.spark.sql(f"""UPDATE {self.table} SET notification_status=:status,provider_message_id=:message,
            notification_error_code=:error,artifacts_json=:artifacts
            WHERE run_id=:key AND notification_status='EMAIL_SENDING'""", args={
                'key': key, 'status': status, 'message': message_id, 'error': error,
                'artifacts': json.dumps(artifacts or [], ensure_ascii=False),
            }).collect()
