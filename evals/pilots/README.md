# PANINI Harbor pilot results

These jobs ran locally with Harbor 0.24.0 and Docker and were uploaded publicly to Harbor Hub.
They are six small custom engineering tasks, each repeated three times. They are not Terminal-Bench or a comparison against Codex/Claude.

The corrected run passed 18/18 independent oracles and completed all 18 CLI turns. Its estimated API cost was $0.0870302.
The preliminary run also passed its oracles, but 15 CLI turns stayed unverified because of a test-fixture error. It is excluded from primary claims.
Both runs cost an estimated $0.1978898 together. Estimates use the recorded price table; provider billing is authoritative.

[Full summary](summary.json) · [Packaged Windows UAT](verification.json)

Credentials were redacted, private sessions and encrypted reasoning omitted, and recovery metadata recomputed before upload.
Original trial IDs, oracle rewards and timestamps were retained. Uploaded job titles identify each phase.

## Preliminary pilot (excluded)

[Detailed report](pilot-20261011-101244/report.json)

| Task | Trial | CLI outcome | Oracle | Harbor Hub |
| --- | ---: | --- | --- | --- |
| fix-add | 1 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/10de41e3-4180-4621-ad5e-879b4ff887b1) |
| fizzbuzz | 1 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/20e8386b-85c5-4e21-ae5f-9cb7e356f239) |
| pagination-off-by-one | 1 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/1b01934a-aef8-415f-9412-9203dc099787) |
| parse-config | 1 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/819ce4cd-91a9-45f1-87b7-c02529362743) |
| rename-across-files | 1 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/23474bfc-0fb6-41d9-bfdd-b2dd392bf697) |
| verify-after-diagnostic | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/fe81da15-a743-400e-b718-5f931c74b90a) |
| fix-add | 2 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/33115d6d-322f-4435-b9ca-982e69e46c99) |
| fizzbuzz | 2 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/d0a135c7-a90b-4713-914c-f09921b159b6) |
| pagination-off-by-one | 2 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/44d095c4-299b-446f-a551-0bc54aecbcb0) |
| parse-config | 2 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/e72afd29-ccbc-4c0f-9ffe-81a7dd6ce192) |
| rename-across-files | 2 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/eb7845fc-ff22-491e-83d4-c277181a692f) |
| verify-after-diagnostic | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/403ee6c1-9c13-4bfe-b670-cbd1fef6478d) |
| fix-add | 3 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/a1ed040e-8299-4c97-86ad-db84f9cf0bae) |
| fizzbuzz | 3 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/2cf2edf9-ea1c-4a80-8dc8-bb2c3f260548) |
| pagination-off-by-one | 3 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/c2e2caa7-c5fd-4b44-9e97-a932f808d4ba) |
| parse-config | 3 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/87d4e5f1-88ea-4e3e-8a2f-9f79cad4aef1) |
| rename-across-files | 3 | unverified | PASS | [Open job](https://hub.harborframework.com/jobs/a748c99c-f842-47a7-a0ee-8b1d8b673e27) |
| verify-after-diagnostic | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/1aa57719-c620-47bb-a477-f92f1e18b6d1) |

## Corrected pilot (primary)

[Detailed report](pilot-20261011-102353/report.json)

| Task | Trial | CLI outcome | Oracle | Harbor Hub |
| --- | ---: | --- | --- | --- |
| fix-add | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/169e0a7d-afc0-4ffa-bc49-824bbce4d5d9) |
| fizzbuzz | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/5e00d5ea-185c-4cae-8d93-b3b585e71f55) |
| pagination-off-by-one | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/8e3ad184-4a8f-4b9f-9d26-0a3333b31038) |
| parse-config | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/42877633-8e89-4f21-b5ba-b848a5578041) |
| rename-across-files | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/a36c7268-3289-4274-b2d5-e0a7704cbadf) |
| verify-after-diagnostic | 1 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/0fa8a7a7-9aaa-4ec8-b186-e293acfdd409) |
| fix-add | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/828235b6-35a5-4925-97a0-e7ae2b497108) |
| fizzbuzz | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/0a00268c-2d1c-486d-b436-3c717db1a394) |
| pagination-off-by-one | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/4457d2fa-0b3e-4b1c-bc34-5ff9ab4bd216) |
| parse-config | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/312d7e31-80ad-434b-acd0-1b98d41368ed) |
| rename-across-files | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/876c169e-72fd-4757-b1f3-cd4c763bd501) |
| verify-after-diagnostic | 2 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/7a5183b5-6206-4846-b2aa-946a1ead7658) |
| fix-add | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/5ba89cbd-fbbd-422c-92d8-c86570f9c2ec) |
| fizzbuzz | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/8d9222eb-4a44-422b-9781-3c37c93ef6f9) |
| pagination-off-by-one | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/4fa86d05-905b-4ed9-8591-63e6fd6c041c) |
| parse-config | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/1d745ff4-f137-4b79-8a35-c4579224d9c4) |
| rename-across-files | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/c13e0fa7-7ef6-45e9-8036-e0316b0cfb82) |
| verify-after-diagnostic | 3 | completed | PASS | [Open job](https://hub.harborframework.com/jobs/852a90b2-91c2-47fe-b688-4592ceb866f9) |
