# Contract schemas

`kit-schemas/` is an unmodified copy of `reference/schemas/` from the City Brain integration kit
(the Brain's `brain/contracts/schemas/`). Our contract tests (`tests/brain/test_contract.py`)
validate every body the Guardian sends to the Brain against `external-verdict`,
`external-quarantine` and `external-status`, and check that the kit's example bodies parse in the
adapter. When the Brain team changes a schema, copy the new file here and re-run the tests.
