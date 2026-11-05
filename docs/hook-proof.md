# Hook proof: does `pre_llm_call` actually change anything?

Measured 2026-11-05T13:46:27Z on one machine, basekit 1.0.0, model `deepseek/deepseek-v4.1-flash`,
against live Base mainnet (chain 8453). Every number below is copied out of
`docs/evidence/*.json` by `scripts/build_hook_proof.py` — none is hand-typed, and
you can re-run the whole thing with the commands at the bottom.

This exists because "it has a hook" is not evidence. A hook is worth shipping if
it fires when it should, stays quiet when it shouldn't, and measurably changes the
turn it fires on. So this measures all three, and states plainly what it does not show.


## 1. The gate: a pure function of the user message

The same function Hermes calls before the LLM, called directly. No model involved,
no network call to decide, no cost.

| user message | hook returns |
| --- | --- |
| `what is base gas right now?` | **INJECT** |
| `write a haiku about a dog` | **NONE** |
| `why are my block sizes so large in the prompt` | **NONE** |
| `token budget and block sizes in my prompt` | **INJECT** |
| `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | **INJECT** |
| `0x3cb57f2ed0bbad21fb8b489d19d5e143f92131ab84b806e9586e59127855b49d` | **INJECT** |

A Base term on its own is enough. An unrelated turn injects nothing. One ambiguous
word ("block") is not enough on its own — two of them ("token" + "block") are —
because those words are everywhere in agent conversation and would otherwise fire on
every other turn. A bare address or transaction hash always counts.


## 2. What the model actually receives

The exact return value, captured — not a reconstruction:

```text
basekit live Base read (chain 8453, read-only, block 53548573): gas 0.006 gwei · ETH $2389.63 · a plain transfer costs ~$0.000301. Use the base_* tools for wallet, receipt, token, transfer and approval facts; every basekit answer states the block it read.
```
Under 200 tokens of prompt, cached for 60 seconds so consecutive Base turns cost one
RPC read, not one per turn. It fails open: if the RPC is unreachable the hook returns
nothing and the turn proceeds exactly as it would without basekit installed.


## 3. Same question, hook on vs off

Question, sent unchanged to `hermes -z` six times (three with the hook enabled, three
with it disabled), same model, same chain:

> What is Base gas right now, and what would a plain ETH transfer cost in USD? Answer in one line.

| run | hook ON api_calls | hook ON input_tokens | hook OFF api_calls | hook OFF input_tokens |
| --- | --- | --- | --- | --- |
| 1 | 1 | 22,159 | 2 | 44,422 |
| 2 | 1 | 22,100 | 3 | 67,163 |
| 3 | 2 | 44,443 | 3 | 67,535 |
| **mean** | **1.33** | **29,567** | **2.67** | **59,706** |

With the hook, 2 of 3 runs answered in a single
model call with zero tool round-trips. Without it, 3 of
3 runs needed more than one. Mean input tokens 29,567 vs 59,706 — the hook roughly halves
the token cost of that question, because state already in the prompt is cheaper than a tool
result re-sent with the whole conversation behind it.

A hook-on answer, verbatim:

```text
Base gas is 0.006 gwei (block 53548498) with ETH at $2,389.63, so a plain 21,000-gas ETH transfer costs about $0.000301.
```
Its block number (53548498) came out of the injected line — that run made one model call, so nothing fetched it.


## 4. Unrelated turns pay nothing

A non-Base prompt with the hook installed measured **21,966** input tokens
in 1 model call, against a **21,960**-token baseline measured on the same
instance for a trivial prompt with no basekit injection (`docs/evidence/baseline-usage.json`).
That is a +6-token difference, i.e. no injection happened: the gate
returned `None` and the prompt is unchanged. Installing basekit does not tax turns that have
nothing to do with Base. Note the lever this gives you — the hook adds under 200 tokens when it
fires and zero when it does not, while the tool it can save costs tens of thousands.


## 5. What this does not show

- **It does not show the model could not have known otherwise.** With the hook off, the
  agent simply calls `base_network` and gets the same answer. The win is one fewer model
  call, roughly half the tokens, and chain state present *before* the model chooses
  anything — not a capability the model lacks.
- **It does not show a tool call is always avoidable.** One of three hook-on runs still
  fetched. That is correct behaviour — the hook is a head start, not a replacement.
- **n is 3 per arm on one machine, one chain state, one model.** These are small-sample
  numbers and are reported as such. They are enough to show the mechanism works and the
  gate is real; they are not a benchmark.
- **No correctness difference was measured.** Both arms produced correct answers, which
  is the honest result — the interesting difference is cost, not truth.


## 6. Reproduce it

```bash
# the gate: no model needed, and the decision itself needs no network
cd ~/.hermes/plugins/basekit    # wherever basekit is installed
python3 -c "import __init__ as b; print(b._pre_llm_call(user_message='what is base gas right now?'))"

# the A/B: hook on (3 runs)
hermes -z "What is Base gas right now, and what would a plain ETH transfer cost in USD? Answer in one line." \
  --usage-file /tmp/on.json

# the A/B: hook off — add to <HERMES_HOME>/config.yaml, run again, then remove it
cat >> ~/.hermes/config.yaml <<'YAML'
plugins:
  entries:
    basekit:
      settings:
        inject_chain_context: false
YAML
```
`scripts/build_hook_proof.py` regenerates this file and the tables from
`docs/evidence/hook-ab.json` + `docs/evidence/hook-gate.json`.


## 7. Raw evidence

| file | sha256 |
| --- | --- |
| `docs/evidence/hook-ab.json` | `65c6ec144e162082d36f75ca5c85c29e11d56996c7b0a40fa5efdc523702b52e` |
| `docs/evidence/hook-gate.json` | `749fa550608769e63cc4750a4618b0b33ffa1ffdd9e8c4004f7e9169b441adab` |
| `docs/evidence/baseline-usage.json` | `65f4a785f2262909650e4cfd2c9ab26996a52fa73ebe23d42b8b8080d9290d1b` |

An 82-second walkthrough of these results ships with the repo: `docs/hook-proof.mp4`
(1600x900, 82.0s, h264). It renders these captures frame by frame — the numbers in it are the
numbers above, so it adds narration rather than evidence.

