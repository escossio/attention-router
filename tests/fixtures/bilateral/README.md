# Synthetic bilateral fixtures

Every person, contact, telephone identifier, LID, account, message, event and document in this directory is authored test data. These fixtures are not exports of WhatsApp, a personal address book or production storage.

- The `550000...` telephone values use a deliberately invalid Brazilian area code; `100000...@lid` values are simulated provider identifiers.
- Audio/media bytes are generated in memory by the test adapter. They contain no recordings, photographs or personal files.
- Google People/OAuth and transcription providers use test doubles. Session keys and signatures in API tests are generated for disposable test identities.
- Names and dialogue in the language corpora describe fictional scenarios. Development, validation and final corpus are separate; the final corpus and interpreter hashes are frozen in `language-evaluation-freeze.json`.
- Generated demonstrations, runtime inventories, local paths and operational logs remain outside the repository. Tests regenerate synthetic evidence in temporary directories.

Do not replace a fixture with an operational capture. No test grants execution authority or authorizes personal-data processing.
