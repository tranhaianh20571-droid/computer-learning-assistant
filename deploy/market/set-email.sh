#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=/etc/studypal-market.env

read -rp "QQ 邮箱（留空跳过）: " QQ_EMAIL
QQ_CODE=""
if [ -n "${QQ_EMAIL}" ]; then
  read -rsp "QQ 邮箱授权码（不是 QQ 密码）: " QQ_CODE
  echo
fi

read -rp "Gmail（留空跳过）: " GM_EMAIL
GM_PASS=""
if [ -n "${GM_EMAIL}" ]; then
  read -rsp "Gmail App Password: " GM_PASS
  echo
fi

if [ -z "${QQ_EMAIL}" ] && [ -z "${GM_EMAIL}" ]; then
  echo "至少配置一个邮箱。" >&2
  exit 1
fi

sudo -n tee "${ENV_FILE}" >/dev/null <<EOF
MARKET_BASE_URL=https://studypal.tailf6f37a.ts.net
QQ_SMTP_HOST=smtp.qq.com
QQ_SMTP_PORT=465
QQ_SMTP_SSL=true
QQ_SMTP_TLS=false
QQ_SMTP_USER=${QQ_EMAIL}
QQ_SMTP_PASSWORD=${QQ_CODE}
QQ_FROM_EMAIL=${QQ_EMAIL}
GMAIL_SMTP_HOST=smtp.gmail.com
GMAIL_SMTP_PORT=465
GMAIL_SMTP_SSL=true
GMAIL_SMTP_TLS=false
GMAIL_SMTP_USER=${GM_EMAIL}
GMAIL_SMTP_PASSWORD=${GM_PASS}
GMAIL_FROM_EMAIL=${GM_EMAIL}
EOF

sudo -n chmod 600 "${ENV_FILE}"
echo "已写入 ${ENV_FILE}（权限 600）。密钥没有回显。"
