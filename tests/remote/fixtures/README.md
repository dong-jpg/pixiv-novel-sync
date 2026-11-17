# InstaBox schema fixtures

These files contain synthetic values constructed from the public schema. They
are not account captures and contain no credentials or user data.

- [Machine](https://instabox.sh/docs/api-reference/machines/get-a-sandbox.md)
- [Operation](https://instabox.sh/docs/api-reference/operations/get-an-operation-the-progress-of-a-sandbox-change.md)
- [Cooldown](https://instabox.sh/docs/machines/auto-pause.md)

The wire field `Operation.type` maps to the internal `OperationView.action`.
Only fields consumed by the decoder are present in the machine fixture.
Passing these tests establishes compatibility with these documented schemas,
not successful execution against a real machine.
