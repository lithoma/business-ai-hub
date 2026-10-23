"""Shareable integration examples; running this file performs OFFLINE checks only.

Call helpers only inside the existing authenticated application's request context.
Never expose this as a standalone service or trust a browser-supplied Identity.
"""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'business-ai-hub'))

from fastapi import HTTPException
from hub_auth import (
    Identity, _context, business_workspace, current_identity,
    owns, ownership_params, ownership_sql,
)


def require_personal_identity():
    """These examples deliberately reject shared trial and shared App modes."""
    user = current_identity()
    if user.mode not in ('oauth_user', 'obo_user', 'apps_obo', 'local_cli'):
        raise HTTPException(403, '此操作示例要求个人授权，不能使用共享业务身份。')
    return user


def bind_creator(validated_task):
    """After normal form validation, override any client-supplied ownership."""
    user = require_personal_identity()
    task = deepcopy(validated_task)
    task.setdefault('report', {})['security'] = user.security
    task['owner'] = user.label
    return task


def owner_filter():
    """Append this WHERE predicate BEFORE pagination in a metadata SELECT.

    Column name is fixed internal code. Bind the returned values through the SQL
    API; never interpolate them. Table names come from validated server config.
    The metadata client must not execute business SQL.
    """
    require_personal_identity()
    return ownership_sql('report_config'), ownership_params()


def require_owned_task(task):
    require_personal_identity()
    if not owns(task):
        # Same response for missing/foreign records; do not disclose their data.
        raise HTTPException(404, '任务不存在或不可访问。')


def evaluate_condition(task, warehouse_id, date_parameters):
    """Run stored condition SQL under the current user's business credentials.

    Caller must retain the application's existing SQL-review and readiness checks;
    this ownership example does not replace the full task execution entrypoint.
    No metadata/storage SDK or shared fallback is used here.
    """
    require_owned_task(task)
    from condition_sql import execute_readonly
    return execute_readonly(
        business_workspace(), warehouse_id, task['trigger']['sql'], date_parameters,
    )


def offline_example():
    """Fixture identities only: no credentials, network, model or SQL execution."""
    workspace = 'https://example.invalid'
    alice = Identity('fixture-alice', workspace, 'Alice', 'oauth_user', object())
    bob = Identity('fixture-bob', workspace, 'Bob', 'oauth_user', object())
    marker = _context.set(alice)
    try:
        task = bind_creator({'owner': 'forged', 'report': {'security': {'creator_subject': 'forged'}}})
        assert task['report']['security'] == alice.security
        assert business_workspace() is alice.client
        require_owned_task(task)
        predicate, values = owner_filter()
        assert ':auth_subject' in predicate and values['auth_subject'] == alice.subject

        _context.set(bob)
        assert business_workspace() is bob.client
        try:
            require_owned_task(task)
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError('Cross-user task access was allowed')

        for mode in ('shared_trial', 'databricks_app'):
            _context.set(Identity('shared', workspace, 'Shared', mode, object()))
            try:
                require_personal_identity()
            except HTTPException as error:
                assert error.status_code == 403
            else:
                raise AssertionError('Shared identity accepted as personal authority')
    finally:
        _context.reset(marker)
    print('Offline example passed: creator binding, user separation, shared-mode rejection. No network calls.')


if __name__ == '__main__':
    offline_example()
