# Contributing

Contributions that improve correctness, safety, testability and source abstraction are welcome.

## Workflow

1. Create a focused branch.
2. Add or update tests for the behavior being changed.
3. Run the complete verification sequence from `README.md`.
4. Keep fixtures synthetic and free of customer or supplier data.
5. Explain operational and migration impact in the pull request.

## Boundaries

Do not contribute:

- credentials, account identifiers or private endpoints;
- downloaded procurement documents or real customer reports;
- source-provider documentation that cannot be redistributed;
- production profiles, chat IDs, logs or database dumps;
- generated agent memory or unrelated commercial materials.

New dependencies must include a license review and an update to `THIRD_PARTY_NOTICES.md`.
