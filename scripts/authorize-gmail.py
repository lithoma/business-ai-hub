#!/usr/bin/env python3
"""One-time PERSONAL Gmail project OAuth. Does not send mail or access the inbox.

Run yourself with a Google Desktop OAuth client JSON downloaded from your project.
Credentials are written outside this repository with mode 0600 and never printed.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path.home() / '.config/business-ai-hub/gmail-credentials.json')
    parser.add_argument('--expected-email', default='', help='Verify the selected Google account before saving project credentials')
    args = parser.parse_args()
    from google_auth_oauthlib.flow import InstalledAppFlow
    scopes = ['https://www.googleapis.com/auth/gmail.send']
    if args.expected_email: scopes += ['openid','https://www.googleapis.com/auth/userinfo.email']
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), scopes=scopes)
    credentials = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True,
        access_type='offline', prompt='consent', timeout_seconds=600,
        authorization_prompt_message='Complete project Gmail authorization in the opened browser. No email has been sent.',
        login_hint=args.expected_email or None,
        success_message='Gmail authorization received. No email has been sent. You can close this tab.')
    if not credentials.refresh_token:
        raise RuntimeError('Offline authorization did not return a refresh token')
    email = ''
    if args.expected_email:
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_oauth2_token
        info = verify_oauth2_token(credentials.id_token,Request(),audience=credentials.client_id)
        email = info.get('email','')
        if not info.get('email_verified') or email.casefold() != args.expected_email.casefold():
            raise RuntimeError('Selected Google account does not match the expected sender; no credentials saved or email sent')
    target = args.output.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    if target.is_relative_to(repo):
        raise ValueError('Credential output must be outside the repository')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation avoids replacing previously configured credentials.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'client_id': credentials.client_id, 'client_secret': credentials.client_secret,
                   'refresh_token': credentials.refresh_token, 'authorized_email':email}, stream)
    print(f'Credentials saved securely to {target}. No email was sent. Do not paste this file into chat or commit it.')


if __name__ == '__main__': main()
