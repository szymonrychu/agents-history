{{/* Mount points of the service and ingest tokens files (api only; projected to "tokens"). */}}
{{- define "agents-history.serviceTokensDir" -}}/var/run/secrets/service{{- end }}
{{- define "agents-history.ingestTokensDir" -}}/var/run/secrets/ingest{{- end }}

{{/* Names of the listener tokens Secrets; fail when the listener is on without one. */}}
{{- define "agents-history.serviceTokensSecret" -}}
{{- required "serviceListener.tokensSecret.name is required when serviceListener.enabled" .Values.serviceListener.tokensSecret.name -}}
{{- end }}
{{- define "agents-history.ingestTokensSecret" -}}
{{- required "ingestListener.tokensSecret.name is required when ingestListener.enabled" .Values.ingestListener.tokensSecret.name -}}
{{- end }}

{{/* Settings ConfigMap data no secret values. Fails on a bad env key. */}}
{{- define "agents-history.config" -}}
HISTORY_HTTP_PORT: {{ .Values.components.api.port | int | toString | quote }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "HISTORY_" $k) }}
{{- fail (printf "agents-history: env key %q must start with HISTORY_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* Settings of the api container only, as container env entries. Call with the root context. */}}
{{- define "agents-history.apiEnv" -}}
{{- $v := .Values }}
{{- if $v.serviceListener.enabled }}
- name: HISTORY_SERVICE_PORT
  value: {{ $v.serviceListener.port | int | toString | quote }}
- name: HISTORY_SERVICE_TOKENS_FILE
  value: {{ printf "%s/tokens" (include "agents-history.serviceTokensDir" .) | quote }}
{{- end }}
{{- if $v.ingestListener.enabled }}
- name: HISTORY_INGEST_PORT
  value: {{ $v.ingestListener.port | int | toString | quote }}
- name: HISTORY_INGEST_TOKENS_FILE
  value: {{ printf "%s/tokens" (include "agents-history.ingestTokensDir" .) | quote }}
{{- end }}
{{- end }}
