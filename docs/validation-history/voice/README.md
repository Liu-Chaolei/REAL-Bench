# Gaia2 voice scoring audit history

These reports were imported from `origin/tau-backup` commit `4961bee` and are
the provenance for the audited literal and location rules in
`src/tau2/domains/gaia2/location_rules.py`.

The reports describe voice/ASR risks for the original Gaia2 source tasks. They
do not change the current 1100-task async data seal. Async variants resolve
their scoring provenance through `scenario.source_id` and
`scenario.async_metadata.source_task_id`.

The reports are historical evidence. Runtime behavior is defined by the
current scoring implementation and its tests.
