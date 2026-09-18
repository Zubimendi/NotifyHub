# The story behind NotifyHub

*Use this as a base for a LinkedIn post, a Medium article, or interview
talking points. Rewrite it in your own voice — this is scaffolding, not a
script.*

## Title

**Retrying a Failed Send Is Easy. Knowing Whether It Actually Failed
Isn't.**

## Medium tags

1. `Software Architecture`
2. `Backend Development`
3. `System Design`
4. `Distributed Systems`
5. `Python`

## LinkedIn tags / hashtags

```
#SoftwareEngineering #SystemDesign #BackendDevelopment #DistributedSystems
#SoftwareArchitecture #Python #FastAPI #API
```

Use a subset (5–6) if the full list feels like keyword-stuffing —
`#SystemDesign #SoftwareArchitecture #DistributedSystems
#BackendDevelopment #Python` is a reasonable minimal set.

## The short version (LinkedIn post)

Every notification system eventually adds "retry on failure, fail over
to a backup provider," and almost none of them ask the sharper question
first: when a provider call times out, do you actually know it failed?
You don't. You know the response never arrived. The request might have
been fully processed on the other end. Retry the same provider and you
might double-send. Fail over to a different one immediately — the
instinctive "fix" — and you're *more* likely to double-send, not less,
because the new provider has no way to know the first one might already
have delivered it.

I built **NotifyHub**, a unified notification gateway in Python where
every provider failure gets sorted into one of three categories before
anything happens next: genuinely ambiguous (retry the *same* provider,
with an idempotency key, so the retry is safe even if the first attempt
actually landed), definitively the provider's problem (move to the next
one in the chain), or definitively the recipient's problem (stop
entirely — no other provider will succeed against an address that
doesn't exist). It also refuses to trust a preference or suppression
check made when a notification was first queued — a digest batch built
over an hour gets re-checked against whatever's true the moment it
actually flushes, not whatever was true when the first event in it
arrived.

The mock providers run as real, separate network services specifically
so the "ambiguous timeout" scenario is a real one — a genuine request
that genuinely times out, with its real fate on the other end genuinely
unknown to the caller, not a stub pretending to be uncertain.

Repo: `<your-fork-url>`

---

## The longer version (Medium article)

### Retrying a Failed Send Is Easy. Knowing Whether It Actually Failed Isn't.

### The question most retry logic never asks

"Add retries and a fallback provider" is close to the first thing anyone
building a notification system reaches for, and it's not wrong,
exactly — it's incomplete in a way that only shows up under real network
conditions, not in a demo. A provider call fails. What actually
happened? Most retry logic doesn't ask, because most of the time the
answer doesn't matter: an HTTP client library reports "error," the
system retries, and either it works this time or it doesn't. Fine for
an idempotent read. Not fine for "send this email," because sending it
twice isn't a no-op — it's a second email in someone's inbox, and the
kind of small, embarrassing bug that erodes trust in a product in a way
that's hard to point back to a specific root cause, because it doesn't
happen every time, only under the exact network conditions that make it
invisible in testing.

The failure that actually causes this isn't a clean rejection. A
provider that returns a clear "invalid request" or "rate limited" told
you something — you know what happened, and you can decide what to do
next with confidence. A provider that times out told you *nothing*. The
request might have been received, processed, and successfully delivered
on the other end, with only the confirmation lost somewhere on the way
back to you. From where you're standing, "it definitely failed" and "it
might have completely succeeded" look identical: a timeout. Most retry
logic treats every non-success the same way, which means it's
implicitly betting on the second, worse interpretation every single
time — and paying for that bet with occasional duplicate sends nobody
budgeted for.

### Three categories, not one

The fix starts with refusing to collapse every kind of failure into one
bucket. A provider's response — or lack of one — actually tells you one
of three meaningfully different things, and each one calls for a
different next move.

If the failure is genuinely ambiguous — a timeout, a dropped connection,
anything where you can't tell what happened on the other end — the right
move is to retry, but against the *exact same provider*, carrying a
stable identifier that says "this is still the same request, not a new
one." A provider that honors that identifier will recognize a retry as
a duplicate of something it may have already processed and simply
return the same result instead of sending again. That single design
choice is what makes retrying an ambiguous failure safe rather than
doubling down on the same coin flip.

If the failure is definitively the provider's own problem — an explicit
rejection, a rate limit, an account issue — retrying the same provider
again is pointless; the answer isn't going to change. That's the moment
to move to the next provider in line, cleanly, with nothing to be
uncertain about.

And if the failure is definitively about the *recipient* — an address
that's malformed or doesn't exist — trying a different provider is
actively the wrong move. No provider is going to successfully deliver
to an address that isn't real. The correct response is to stop
immediately and report a real failure, not to burn a request against a
second provider chasing an outcome nothing can produce.

Getting this distinction right the first time matters more than it
sounds like it should, because the failure mode of getting it wrong is
silent. A system that doesn't distinguish these three cases doesn't
crash or throw an obvious error — it just occasionally sends the same
message twice, for reasons that look, from any individual incident,
like bad luck rather than a design gap.

### Real providers don't all give you the same tools

Building this honestly meant checking something worth stating plainly
rather than assuming: not every real notification provider actually
supports the "safe to retry with this identifier" mechanism the whole
first half of this design leans on. Several major providers do —
Resend, Brevo, and payment-adjacent APIs like Moov all accept a
client-supplied idempotency key on send, exactly as described. SendGrid,
one of the most widely used email APIs in the industry, notably does
not offer this on its send endpoint at all. A system whose entire
duplicate-prevention story assumes every provider plays along would
quietly have a much weaker guarantee against one specific, popular
provider than its own documentation implied.

So the honest version of this design says so directly: NotifyHub always
computes and records its own idempotency key, for every provider,
whether or not that provider actually uses it. For providers that
support it, the guarantee described above is real and strong. For ones
that don't, a retry against that specific provider carries a genuinely
higher — still bounded, still rare, but real — risk of a duplicate. The
actual, honest claim this whole system reaches is at-least-once
delivery with duplicates kept rare, not a guarantee of exactly-once,
and pretending otherwise for the sake of a cleaner architecture diagram
would be exactly the kind of overclaim this discipline is supposed to
prevent.

### A decision made an hour ago isn't the same as a decision made now

The other place this project refuses a shortcut is batching. Grouping
related notifications into one digest instead of sending each
individually is good for the recipient and good for delivery volume,
but it introduces a specific, easy-to-miss correctness gap: a digest
window can stay open for an hour, and a person's preferences — or their
suppression status, if they hit "unsubscribe" partway through — can
change at any point during that hour. A system that only checks
preferences once, when the first event in the batch arrives, will
happily send a digest to someone who explicitly opted out ten minutes
before it actually went out, because the check that mattered was the
stale one.

The fix is unglamorous and exactly as important as it sounds: check
again, right before sending, every single time, no exceptions for
"we already checked this earlier." It's a small amount of extra work
per send, paid consistently, in exchange for never having a customer
support ticket that starts with "I unsubscribed and you emailed me
anyway."

### Recognizing infrastructure you've already built

The last piece worth naming is what this project deliberately doesn't
build: its own retry-with-backoff scheduler. Retrying a failed job after
a delay, with an increasing backoff, up to some maximum number of
attempts, is a generic problem — and it's one this same portfolio
already solved well, as its own dedicated project. Rebuilding that
machinery a third time here, just because it's convenient to have it
live in the same codebase, would be effort spent re-proving something
already proven, at the cost of the time that should go toward the part
of this project that's actually novel: the ambiguous-versus-definitive
classification itself. So each individual provider-attempt becomes one
job on that existing system, and this project's own code stays focused
on exactly the domain logic that's actually its job — what kind of
failure this was, and what should happen next because of it.

### What's honestly not done

This project is handed off at the architecture layer — every mechanism
above is specified precisely enough to build directly from, and the
tests that would prove each claim, including a mock provider built to
produce genuine, real-network ambiguous timeouts rather than a stub
pretending to be uncertain, are written out with the same precision.
None of it exists as running code yet. The residual duplicate-send risk
under a provider without idempotency-key support is named honestly as
an accepted limitation, not solved — and a real provider integration,
when it eventually replaces the mocks, inherits that same honest
constraint rather than a promise the architecture can't actually keep.

### Conclusion: the interesting failure was never the obvious one

It's tempting to think the hard part of a notification system is
sending the message — picking an API, formatting the payload, handling
the response. The actual hard part shows up exactly once something goes
wrong, and specifically once something goes wrong in a way that doesn't
clearly say what happened. A clean rejection is easy to handle
correctly; a silence is not, and silence is what real networks actually
produce far more often than anyone building against a reliable local
demo environment tends to expect. Treating every kind of not-a-success
identically is the single most common, most invisible mistake in this
entire space, and the fix isn't more retries or a cleverer failover
policy — it's asking, honestly, for every single failure: do I actually
know what happened here, or am I about to guess and hope the guess is
cheap to be wrong about?

---

## Talking points for an interview

1. **Lead with the sharper question, not "I added retries."** "A timeout
   doesn't tell you the request failed — it tells you nothing, and most
   systems treat 'nothing' the same as a clear failure, which is exactly
   how duplicate sends happen" is a much stronger opening than
   describing a retry-and-failover feature.
2. **Explain the three-way classification precisely** — ambiguous
   (retry same provider, idempotency key), definitive-provider
   (failover), definitive-recipient (stop entirely) — and why
   conflating any two of them causes a specific, real problem. This
   distinction is the single best differentiator in this project's
   story.
3. **Be candid that not every real provider supports idempotency keys**
   — naming SendGrid specifically as a major provider that doesn't, and
   explaining what that means for the actual guarantee the system can
   make, is a much stronger signal of real understanding than an
   architecture diagram that implies a uniform, unconditional guarantee.
4. **Explain the freshness check on digest batching** — re-checking
   preferences and suppressions at the moment of send, not enqueue —
   and why a batching window makes this matter more, not less, than it
   would for an immediate send.
5. **Point at the decision to delegate retry scheduling to an existing
   system** rather than rebuilding it — this is a strong signal of
   engineering judgment: recognizing when a problem is already solved
   elsewhere and choosing not to re-solve it is as valuable a skill as
   solving new problems well.

## Suggested post formats

**Short (LinkedIn/X):**
> Built a unified notification gateway in Python where every provider
> failure gets sorted into one of three categories before anything
> happens next: ambiguous (a timeout — retry the *same* provider with
> an idempotency key, since it might have actually succeeded), the
> provider's problem (fail over to the next one), or the recipient's
> problem (stop entirely — no other provider will fix a broken address).
> Turns out not every major email API even supports idempotency keys —
> SendGrid notably doesn't — so the system computes and records its own
> regardless and is honest that the actual guarantee is at-least-once,
> not exactly-once. Digest batches re-check preferences and suppression
> at the moment they actually flush, not whenever the first event in
> them arrived. Open source: `<link>`
>
> #SystemDesign #SoftwareArchitecture #DistributedSystems
> #BackendDevelopment #Python

**Medium article structure:** *Retrying a Failed Send Is Easy. Knowing
Whether It Actually Failed Isn't.* → the question most retry logic never
asks → three categories, not one → real providers don't all give you the
same tools → a decision made an hour ago isn't the same as a decision
made now → recognizing infrastructure you've already built → what's
honestly not done → conclusion. `ARCHITECTURE.md` §1–3 and §6 can be
lifted almost directly into the technical middle of the article.
