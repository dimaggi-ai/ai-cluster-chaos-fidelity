# Drill plan schema `drill-plan/v1`

One YAML document. Unknown fields are refused at every level. The example [plans/vendor-severance-72h.yaml](plans/vendor-severance-72h.yaml) uses every field.

## Fields

| Field | Required | Rule |
|---|---|---|
| `schema_version` | yes | Exactly `drill-plan/v1`. |
| `mode` | yes | Exactly `plan-only`. No other mode exists. |
| `plan_id`, `title` | yes | Non-empty text. |
| `synthetic` | no | `true` or `false`. |
| `notes` | no | Non-empty text. Not evaluated. |
| `environment.class` | yes | `staging` or `isolated-lab`. Anything containing `prod` is refused as `production_refused`. |
| `environment.description` | yes | Non-empty text. |
| `scenario.kind` | yes | `vendor-control-plane-disconnection` (the only scenario in v1). |
| `scenario.description` | yes | Non-empty text. |
| `scenario.severed_links[]` | yes | `name`, `plane`, `description`. Every `plane` must be `control`. |
| `scenario.excluded_links[]` | yes | Links that stay up. Every `plane` must be `data`. A name may not be both severed and excluded. |
| `window.start`, `window.end` | yes | ISO 8601 date and time **with** a UTC offset, quoted (for example `"2026-12-01T09:00:00Z"`). Compared in UTC. `end` after `start`. |
| `window.duration_hours` | yes | Finite and positive, at most 744, and equal to `end − start` in UTC. |
| `preconditions[]` | yes | `id` (unique) and `description`. |
| `abort_criteria[]` | yes | `id` (unique), `signal`, `comparator` (`>`, `>=`, `<`, `<=`), numeric `threshold`, `unit`, positive `sustained_minutes`; optional `description`. |
| `restore.procedure` | yes | Text for people. Never executed. |
| `restore.max_restore_minutes` | yes | Positive number. |
| `recovery_checks[]` | yes | `id` (unique), `check`, `checked_by_role`: `approver` or `verifier`, never `operator`, and the role must be declared. |
| `roles.approver`, `roles.operator` | yes | Each has `kind: human`, `name`, `reference`. |
| `roles.verifier` | no | Same shape. Needed if a check uses it. |
| `evidence.template` | yes | Exactly `drill-evidence/v1` (see [evidence-template.md](evidence-template.md)). |

Each role must have a different `reference` (compared ignoring case and surrounding spaces). A `name` or `reference` beginning with `agent:`, `bot:`, `service:`, `svc:`, `system:` or `automation:` is refused. These are checks on what the file declares. They do not authenticate anyone, and a valid plan authorises nothing.

## Input limits

| Limit | Value | Reason code |
|---|---|---|
| File size | 64 KiB | `file_too_large` |
| Items in any list or mapping | 32 | `too_many_items` |
| Nesting depth | 8 | `too_deep` |
| String length | 2000 characters | `string_too_long` |
| Key length | 64 characters | `key_too_long` |
| Integer magnitude | 10^15 (literals too long for Python to convert: `yaml_value`) | `number_out_of_range` |
| Window years, after conversion to UTC | 2000 to 2100 | `time_out_of_range` |

`validate_plan.py PATH` reads regular files only. FIFOs, devices, sockets and directories are refused before they are opened (`not_regular_file`). The open is non-blocking and must reach the same file that was checked (`file_changed`). The read stops one byte past 64 KiB, so a file that grows after the size check is refused (`file_too_large`). If an input ever reaches an unexpected error, the report is still JSON, with `internal_error`; no test input reaches it.

The loader is PyYAML's `SafeLoader` with three changes: duplicate keys are refused (`duplicate_key`), anchors and aliases are refused (`yaml_anchor`, `yaml_alias`), and unquoted timestamps stay text so the validator parses them itself. `.inf` and `.nan` are refused (`nonfinite`). Language-specific tags fail to load (`yaml_syntax`). Exactly one document is allowed (`yaml_documents`). The file must be UTF-8 (`encoding`) and mapping keys must be strings (`key_type`).

## Report

```json
{
  "report_version": "drill-plan-report/v1",
  "reports": [{
    "report_version": "drill-plan-report/v1",
    "file": "vendor-severance-72h.yaml",
    "file_sha256": "<sha256 of the bytes>",
    "plan_sha256": "<sha256 of the canonical JSON of the parsed plan>",
    "status": "plan_valid_not_run",
    "reasons": [],
    "limitations": ["..."]
  }]
}
```

- `status` is `plan_valid_not_run` or `invalid`. There is no other value.
- `plan_sha256` is the plan's identity: SHA-256 of the parsed plan as JSON with sorted keys, no spaces, ASCII only. Key order, quoting style and comments do not change it; any change to a value does. It is `null` when the file cannot be parsed. `file_sha256` identifies the exact bytes.
- `reasons` lists one entry per problem, `{path, code, message}`, sorted by path, then code, then message.
- The same file always gives the same report.

## Reason codes

`schema_version`, `mode`, `missing`, `unknown_field`, `executable_field`, `result_field`, `shape`, `text`, `number`, `not_positive`, `list`, `boolean`, `duplicate_id`, `environment`, `production_refused`, `scenario`, `plane`, `link_conflict`, `time`, `window_order`, `duration_mismatch`, `duration_too_long`, `comparator`, `role_not_human`, `roles_not_distinct`, `check_not_independent`, `role_undeclared`, `evidence_template`, `time_out_of_range`, plus the input-limit and loader codes above, `not_regular_file`, `file_changed`, `unreadable` and `internal_error`.
