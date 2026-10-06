#!/bin/sh
# Alertmanager can't read environment variables in its config, so it's written here at
# start. With ALERTMANAGER_SLACK_URL (a Slack-compatible incoming webhook) alerts are
# posted there; without it they're only on Alertmanager's own page (127.0.0.1:9093).
set -eu
umask 077
{
  echo "route:"
  echo "  receiver: default"
  echo "  group_by: [alertname]"
  echo "  group_wait: 30s"
  echo "  group_interval: 5m"
  echo "  repeat_interval: 4h"
  echo "  routes:"
  echo "    - matchers: [severity=\"page\"]"
  echo "      receiver: default"
  echo "      repeat_interval: 1h"
  echo "receivers:"
  echo "  - name: default"
  if [ -n "${ALERTMANAGER_SLACK_URL:-}" ]; then
    printf '%s\n' "$ALERTMANAGER_SLACK_URL" > /tmp/slack-url
    echo "    slack_configs:"
    echo "      - api_url_file: /tmp/slack-url"
    echo "        send_resolved: true"
    echo "        title: '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }} ({{ .CommonLabels.severity }})'"
    echo "        text: '{{ range .Alerts }}{{ .Annotations.summary }} · runbook: {{ .Annotations.runbook_url }}{{ \"\\n\" }}{{ end }}'"
  fi
} > /tmp/alertmanager.yml
exec /bin/alertmanager --config.file=/tmp/alertmanager.yml --storage.path=/alertmanager
