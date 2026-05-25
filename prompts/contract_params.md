# Contract Parameter Extraction

You are extracting structured parameters for a Z3 verification template.

## contract_kind
{{contract_kind}}

## contract
{{contract}}

## function source
{{function_source}}

Extract the parameters for this contract_kind. Return ONLY a JSON object.

For "flag_invariant": {"flag_name": "<variable name of the boolean flag>", "check_name": "<description of what check is suppressed when flag is False>", "silent_when_false": true}
For "nullable_contract": {"field_name": "<name of the field or variable that can be None>", "check_name": "<description of what check is skipped when the field is None>", "skips_on_none": true}
For "subset_relation": {"set_a": "<name of the required/expected set>", "set_b": "<name of the provided/actual set>"}
For "ordering": {"first_op": "<name of operation that must run first>", "second_op": "<name of operation that depends on first>", "guard_exists": false}
For "resource_lifecycle": {"resource": "<name of the resource (connection, file, handle, lock)>", "release_guaranteed": false}

Return only JSON. No explanation.
