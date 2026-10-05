# Subscriptions and provider terms

> **Researched 2026-10-05.** Provider policies on subscriptions changed several times in
> 2026. Treat this page as a starting point and check the linked official pages yourself
> before relying on it. Nothing here is legal advice.

## The short answer

- **Subscriptions are not API keys.** Claude Pro/Max, ChatGPT Plus/Pro (with Codex), Google
  AI Pro/Ultra, GitHub Copilot and Cursor are subscriptions. The gateway's normal routes
  (`/v1/chat/completions`, `/v1/messages`) call provider APIs with **API keys**, billed per
  token.
- **Never copy a subscription login token** out of a CLI (`~/.claude/…`, `~/.codex/auth.json`,
  Gemini OAuth, Copilot tokens) into the gateway or any other software. Every provider below
  prohibits it, and Anthropic, Google and GitHub have suspended accounts for it.
- **What *is* documented:** the official coding clients can send *their own* traffic through
  a proxy you run. That's useful for logging and network control of your own use, but the
  gateway's routing, fallback and budgets can't apply to that traffic.

## Two different things

| | A. Credential reuse | B. Pass-through proxy |
|---|---|---|
| What | The gateway uses your subscription token to serve other apps, other people, or its own code | Your official client (Claude Code, Codex), signed in to your own subscription, sends its traffic *through* a proxy |
| Who talks to the model | The gateway, impersonating the official app | The official app; the proxy only relays |
| Fallback, budgets, routing | Would apply, which is the appeal | Can't apply: the token only works against its own provider, and usage counts against your plan |
| Allowed? | **No**, at every provider checked | Documented for Claude Code and Codex; see the per-provider notes below |

## Per provider

| Provider / product | A: reuse the token | B: client through a proxy |
|---|---|---|
| **Anthropic**: Claude Pro/Max + Claude Code | **Not allowed; enforced.** | **Documented.** `ANTHROPIC_BASE_URL` with no gateway credential keeps the claude.ai login active through the gateway. `HTTPS_PROXY` and TLS-inspecting proxies are supported too |
| **OpenAI**: ChatGPT Plus/Pro/Business + Codex CLI | **Not allowed**, except through the official *Sign in with ChatGPT* program (open-source and local personal projects; hosted or paid apps need approval) | **Documented**: custom providers with `requires_openai_auth = true`, `openai_base_url`, and `CODEX_CA_CERTIFICATE` for TLS proxies |
| **Google**: AI Pro/Ultra + Gemini CLI / Antigravity | **Not allowed; enforced with bans in early 2026** | **Not applicable.** Since 2026-06-18, Gemini CLI / Code Assist no longer serve the individual, AI Pro or AI Ultra tiers. API keys are unaffected |
| **GitHub Copilot** | **Not allowed.** GitHub has disabled accounts for "proxy usage" and "credential sharing services" | **Plain HTTP proxy only** (corporate proxy, custom CA). The official programmatic route is the Copilot SDK |
| **Cursor** | **Not allowed** (its ToS bans extraction and reverse engineering) | Not applicable: model requests go from Cursor's servers |
| **xAI** (SuperGrok), **Mistral** (Le Chat Pro / Vibe) | xAI: an account-sharing clause, unverified. Mistral Vibe supports bringing your own API key | Unverified |

### Anthropic: what the terms say

From [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance):

> "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team,
> and Enterprise subscription plans and is designed to support ordinary use of Claude Code and
> other native Anthropic applications."

> "Anthropic does not permit third-party developers to offer Claude.ai login into their own
> applications, or to route requests through Free, Pro, or Max plan credentials on behalf of
> their users. Moreover, developers may not collect, store, or intermediate Claude.ai
> credentials or session tokens."

> "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with
> their own Claude subscription."

From [Claude Code LLM gateway docs](https://code.claude.com/docs/en/llm-gateway):

> "Setting only that variable, without a gateway credential, doesn't replace the subscription.
> Requests still route through the gateway, but a saved claude.ai login remains the active
> credential, so its usage limits and billing apply. Gateways that pass this traffic on to
> Anthropic must forward the OAuth capability in `anthropic-beta`."

The [protocol page](https://code.claude.com/docs/en/llm-gateway-protocol) adds two
requirements for such a gateway. It must not strip `anthropic-beta`, which fails the
requests with 401. And it must inspect bodies "without modifying" them.

How to read this: a proxy that only relays your own Claude Code traffic is documented by
Anthropic. Running one for *other people's* subscriptions, or storing their tokens, falls
under "intermediate … credentials" and isn't allowed. The terms don't state outright where
a personal pass-through gateway sits. A plain `HTTPS_PROXY` in front of the unmodified
binary is the most clearly accepted form.

Anthropic also has an official route for apps on a subscription: the
[Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).
Its terms were changing at the time of writing (the article says changes were paused on
June 15), so check its current status.

### OpenAI: what the docs say

[Codex authentication](https://learn.chatgpt.com/docs/auth) separates "Sign in with ChatGPT
for subscription access" from "Sign in with an API key for usage-based access". It documents
`requires_openai_auth = true` on a custom provider ("You can then sign in with ChatGPT or an
API key"). It also warns: "Treat `~/.codex/auth.json` like a password … Don't commit it,
paste it into tickets, or share it in chat."

[Sign in with ChatGPT](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)
is the official way for third-party apps to use a ChatGPT plan:

> "ChatGPT plan usage is available to open-source projects, personal projects that run
> locally, and selected private apps. If you're building a paid or remotely hosted app, join
> the waitlist."

Unverified: with ChatGPT login, Codex traffic reportedly goes to a ChatGPT backend rather than
`api.openai.com`, so a proxy must route it differently. That comes from community code, not
OpenAI documentation.

### Google, GitHub

- **Google.** The [Gemini CLI terms](https://geminicli.com/docs/resources/tos-privacy/) say:
  "Directly accessing the services powering Gemini CLI … using third-party software, tools,
  or services … is a violation … may be grounds for suspension or termination of your
  account." The [Antigravity terms](https://antigravity.google/terms/) have similar wording.
  See also the [deprecation of the personal tiers in Code Assist](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals).
- **GitHub.** The [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)
  say: "a single login may not be shared by multiple people." The
  [network settings](https://docs.github.com/en/copilot/how-tos/configure-personal-settings/configure-network-settings)
  page documents HTTP proxies and custom CAs.

## What this gateway does about it

- **Credential reuse (A): never.** The gateway reads provider credentials only from
  environment variables holding API keys, and there are no adapters that load subscription
  tokens. Pull requests adding them will be declined, because they would put every user's
  account at risk.
- **Normal routes:** `/v1/messages` currently requires a **gateway key**. Claude Code
  signed in with a subscription sends its OAuth token instead, which the gateway rejects with
  401. To route Claude Code through the gateway, use a gateway key backed by API billing (see
  [Providers and translation](Providers-and-Translation.md#using-it-with-claude-code)).
- **A possible future "pass-through mode":** for one person's own Claude Code or Codex, off
  by default. It would forward the request byte-for-byte, with `Authorization` and every
  `anthropic-*` header untouched, and never store or log the token. It would record only
  metrics: model, usage, latency. Fallback, translation and budgets can't apply to it. It
  isn't built. Whether it fits the terms for your situation is for you to judge from the
  pages above.

## Practical recommendations

1. **For yourself in the official apps:** keep using your subscriptions directly. For heavy
   personal use, flat fees beat per-token pricing.
2. **For apps, bots, batch jobs and other people:** use API keys through the gateway. That's
   where routing, fallback, budgets and cost tracking pay off.
3. **For free capacity:** run local models with Ollama. They're already the last fallback in
   the `fast` and `balanced` chains.
4. **For logging or network control of your own subscription client:** a plain `HTTPS_PROXY`
   with a trusted CA is the most clearly documented option for both Claude Code and Codex.
5. **To build a product on a subscription:** use the official programs (the Claude Agent
   SDK with your plan, Sign in with ChatGPT, the Copilot SDK) and their approval processes.
