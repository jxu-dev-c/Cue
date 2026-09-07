# Translation alignment evaluation

The current implementation uses batches of up to 10 cues / 6,000 source
characters. Two preceding cues supply context, capped at 1,000 characters.
Invalid responses are retried three times, then the batch is halved recursively;
individual cues are the last fallback. Four batches run concurrently.

The earlier single-cue default and punctuation-based context exclusion have been
removed. The results previously recorded for those approaches do not validate
this implementation.

## Verification

- Backend suite: 100 tests run, 3 opt-in tests skipped, no failures.
- Live model: `gpt-5.6-luna`, configured reasoning effort `low`.
- Live evaluation: 13 reported-case cues and 16 fresh travel, everyday-dialogue,
  and software-operation cues, each translated three times.
- 12 initial batch requests, 87 cue outputs; 18 cue-level assertion failures.
- All six live runs produced structurally valid output. Semantic errors therefore
  did not trigger the structural fallback.

For example, all three fresh-example runs swapped the meanings of “Don't restart
the server” and “until the backup finishes.” The Chinese formed a natural sentence
across both cues but assigned its clauses to the wrong timestamps. The original
episode's split-sentence failures also recurred.

**The simpler implementation is complete, but the semantic desync remains
unresolved with the tested model.** These checks do not establish general
translation quality. The fresh examples are newly written test cases, not an
independent real-world subtitle benchmark. No additional provider, review pass,
or episode-specific processing rule has been added.

## Reproduce

```sh
uv run python -m unittest discover -s tests
RUN_TRANSLATION_LIVE=1 uv run python -m unittest tests.test_translation_live
```

The live tests consume API tokens and print the actual translations. They remain
strict so the known semantic failures stay visible rather than being ignored.
