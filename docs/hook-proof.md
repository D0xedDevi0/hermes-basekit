# Hook proof: does `pre_llm_call` actually change anything?

Measured 2026-11-05T16:48:31Z on one machine, basekit 1.0.0, model `deepseek/deepseek-v4.1-flash`, against live
Base mainnet (chain 8453). Every number below is read out of `docs/evidence/*.json`
by `scripts/build_hook_proof.py` — none is hand-typed.


## The short version, including the part that went wrong

| claim | verdict |
| --- | --- |
| the `pre_llm_call` gate fires only on Base turns, and its decision costs nothing | **holds** — measured directly, section 1 |
| the injected line is real, fresh and bounded in size | **holds** — section 2 |
| unrelated turns pay nothing for having basekit installed | **holds** — +6 tokens against baseline, section 4 |
| the hook removes a tool round-trip and halves token cost | **does not hold** — powered A/B (pooled n=24 vs 23, same question): 2.42 vs 2.65 model calls, p=0.17; the data rule out any saving beyond ~0.6 of a call. Section 3 |
| my first published numbers were right | **no** — the toggle wrote to a config file the runtime does not read, so both arms ran with the hook on. Section 5 |

I published a measured claim, then went to verify the mechanism behind it and found the
mechanism was broken. The measurement, the mistake and the correction are all below,
because an experiment you cannot re-run honestly is worth less than no experiment.


## 1. The gate: a pure function of the user message

The same function Hermes calls before the LLM, called directly. No model involved, no
network call needed to decide, no cost.

| user message | hook returns |
| --- | --- |
| `what is base gas right now?` | **INJECT** |
| `write a haiku about a dog` | **NONE** |
| `why are my block sizes so large in the prompt` | **NONE** |
| `token budget and block sizes in my prompt` | **INJECT** |
| `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | **INJECT** |
| `0x3cb57f2ed0bbad21fb8b489d19d5e143f92131ab84b806e9586e59127855b49d` | **INJECT** |

A Base term on its own is enough. An unrelated turn injects nothing. One ambiguous word
("block") is not enough on its own — two of them ("token" + "block") are — because
those words are everywhere in agent conversation and would otherwise fire on every other
turn. A bare address or transaction hash always counts. This part is deterministic: it is
a regex over the user message, so it is re-runnable and falsifiable without a model.


## 2. What the model actually receives

The exact return value, captured — not a reconstruction:

```text
basekit live Base read (chain 8453, read-only, block 53548573): gas 0.006 gwei · ETH $2389.63 · a plain transfer costs ~$0.000301. Use the base_* tools for wallet, receipt, token, transfer and approval facts; every basekit answer states the block it read.
```
Under 200 tokens of prompt, cached 60 seconds so consecutive Base turns cost one RPC read
rather than one per turn, and it fails open: if the RPC is unreachable the hook returns
nothing and the turn proceeds exactly as it would without basekit installed.


## 3. The A/B, run wrong and then run right

### 3.1 The corrected measurement

Same question, same model, same chain; arms **interleaved** ON/OFF so drift and ordering
cancel; the plugin's own `load_settings()` value asserted before every single run:


**the gas/cost question** — `What is Base gas right now, and what would a plain ETH transfer cost in USD? Answer in one line.`

| arm | model calls per run | single-call runs | mean calls | mean input tokens |
| --- | --- | --- | --- | --- |
| hook ON | `[2, 1, 3, 3, 3]` | 1/5 | **2.40** | 53,599 |
| hook OFF | `[3, 3, 2, 3, 3]` | 0/5 | **2.80** | 63,109 |

**a block-number question** — `What block number is Base on right now? Answer with just the number.`

| arm | model calls per run | single-call runs | mean calls | mean input tokens |
| --- | --- | --- | --- | --- |
| hook ON | `[3, 2, 2]` | 0/3 | **2.33** | 52,109 |
| hook OFF | `[2, 2, 2]` | 0/3 | **2.00** | 44,244 |

On the first question the hook-on arm used *fewer* calls (2.40 vs 2.80); on the second it used
*more* (2.33 vs 2.00). Differences that point in
opposite directions are not an effect — they are noise, and at n=3–5 with a model that decides
per turn whether to fetch, that is all this is. **The honest reading: the model calls
`base_network` whether or not the number is already in its prompt** — on the block-number
question the injected line carried the answer verbatim, and every run still fetched it.


One thing in the corrected data does point the way the first claim did: across all 8 valid runs
with the hook on, **1** answered in a single model call; across all 8 runs with it off,
**0** did. That is consistent with injected context occasionally saving a round-trip — and at
this sample size it is just as consistent with chance. It is a hypothesis worth a proper test (more
runs, tool-choice traces), not a result — so section 3.3 tests it.

### 3.2 The first attempt, and why it was invalid

It produced this, and it was published before anyone checked the mechanism:

| arm | model calls per run | mean | mean input tokens |
| --- | --- | --- | --- |
| hook ON | `[1, 1, 2]` | **1.33** | 29,567 |
| hook OFF | `[2, 3, 3]` | **2.67** | 59,706 |

Both arms of that run had the hook **on**. The toggle wrote
`plugins.entries.basekit.settings.inject_chain_context: false` into `~/.hermes/config.yaml`,
but the runtime reads `get_config_path()`, which resolves to `$HERMES_HOME/config.yaml` —
a different file. Nothing was toggled; the 2× difference was run-to-run variance that
happened to line up with the arm labels. Section 5 is the postmortem.


### 3.3 The hypothesis, tested properly

n=20 per arm on the same question, arms interleaved ON/OFF so drift cannot wear an arm label, and the
plugin's own `load_settings()` asserted before every single run. Pooled with the earlier verified n=5:

| arm | n | model calls (mean) | sd | single-call runs | mean input tokens |
| --- | --- | --- | --- | --- | --- |
| hook ON | 24 | **2.42** | 0.64 | 2/24 | 54,163 |
| hook OFF | 23 | **2.65** | 0.48 | 0/23 | 60,017 |

Difference **-0.236 calls**, SE 0.168, Welch t=-1.40 (df=42.5), **p=0.168**. 95% CI [-0.574, +0.103].

Read that honestly, and it is the same verdict as before, now with a bound on it: the direction still
favours the hook — 2 single-call runs with it on, 0 with it off, and about a
tenth fewer input tokens as a consequence of fewer calls — but **p=0.17 is not evidence of an effect**, and the
interval rules out anything larger than roughly a fifth of a call (0.57 of 2.65, ~22%).
For a two-call question that is a tenth of the cost of the turn at most. Anyone claiming a hook of this
shape halves your calls should be asked for an interval, not a mean.


3 of 40 runs wrote no usage file and returned an empty answer, so they are excluded rather
than scored as zero-call turns: `OFF #9` (264s), `ON #10` (23s), `OFF #10` (73s).
A run that produced nothing is not a run that needed no tool call, and quietly counting it as a win is
exactly how a result like this gets faked.


## 4. Unrelated turns pay nothing

A non-Base prompt with the hook installed measured **21,966** input tokens in
1 model call, against a **21,960**-token baseline for a trivial prompt with no
basekit injection (`docs/evidence/baseline-usage.json`) — a +6-token
difference. The gate returned `None` and injected nothing, which is what "costs nothing when
it does not fire" looks like as a number. This claim survives the correction: it is a direct
consequence of the gate in section 1, which is deterministic.


## 5. Postmortem: how a wrong number got published

1. **The toggle was never verified.** I wrote a setting to a config file and assumed the runtime
   read it. It did not. A toggle you have not confirmed is not a toggle — and there was a
   two-second check available the whole time: ask the plugin what it sees.
2. **The tell was in the numbers I published.** A 2× effect at n=3, from a model that decides
   per turn, should have been treated as a hypothesis until the mechanism was confirmed.
3. **The artifact outran the verification.** The doc, the README and a video were built on the
   first result before the toggle was checked. Building on an unverified premise is the
   expensive mistake; the fix cost a re-measure, a rewrite and a correction.
4. **What changed in the method:** assert the plugin's own `load_settings()` value immediately
   before every run, interleave the arms so drift cannot masquerade as treatment, and report
   per-run values so a reader can see the spread instead of trusting a mean.

The wrong capture is kept in `docs/evidence/hook-ab.json` on purpose. Deleting it would hide
the only evidence that the correction happened.


## 6. What the hook is actually worth

Stated at the strength the measurements support:

- **The gate is real.** It fires on Base turns and stays silent otherwise, deterministically,
  with no model call and no network access to decide.
- **The cost is bounded and conditional.** Under 200 tokens when it fires; a measured +6 tokens
  when it does not; never a failed turn, because it fails open.
- **It puts live chain state in front of the model before it decides anything** — not a tool it
  must choose to call, not a skill it must remember to load.
- **It does not make the model cheaper on its own.** On this model and these questions it did
  not remove tool round-trips. Anyone claiming hook-equivalent savings should show a verified
  toggle and per-run numbers, which is exactly what section 3 does for this one.

So the case for basekit being more than a prompt or a skill does not rest on a token saving.
It rests on the surface: six read-only tools, this hook, a `/basekit` command, a `hermes basekit`
CLI command, a desktop pane, a status chip and a dashboard backend — none of which a prompt
can provide.


## 7. Reproduce it

```bash
# the gate: no model needed, and the decision itself needs no network
cd ~/.hermes/plugins/basekit    # wherever basekit is installed
python3 -c "import __init__ as b; print(b._pre_llm_call(user_message='what is base gas right now?'))"

# the A/B - toggle with Hermes' own command, never by hand-editing config.yaml
hermes config set plugins.entries.basekit.settings.inject_chain_context false
python3 -c "import basekit_core as c; print(c.load_settings()['inject_chain_context'])"   # assert it: False
hermes -z "What is Base gas right now, and what would a plain ETH transfer cost in USD? Answer in one line." \
  --usage-file /tmp/off.json
hermes config unset plugins.entries.basekit.settings.inject_chain_context
```
`scripts/build_hook_proof.py` regenerates this page from the captures in `docs/evidence/`.
`--usage-file` reports `api_calls`, which is what makes a tool round-trip countable.


## 8. Raw evidence

| file | what it is | sha256 |
| --- | --- | --- |
| `docs/evidence/hook-ab2.json` | corrected A/B, toggle asserted per run | `5f35a68c48bd232009199da63d64ea2ced63aebf2a13b334ba5849f2b69a5e1c` |
| `docs/evidence/hook-ab.json` | the invalid first A/B, kept as the record | `65c6ec144e162082d36f75ca5c85c29e11d56996c7b0a40fa5efdc523702b52e` |
| `docs/evidence/hook-gate.json` | direct hook calls | `749fa550608769e63cc4750a4618b0b33ffa1ffdd9e8c4004f7e9169b441adab` |
| `docs/evidence/baseline-usage.json` | no-injection baseline prompt | `65f4a785f2262909650e4cfd2c9ab26996a52fa73ebe23d42b8b8080d9290d1b` |

A short walkthrough accompanies the hackathon submission; it renders these captures rather
than adding evidence of its own.

