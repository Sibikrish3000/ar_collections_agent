# Predictive analytics on concrete defects — what I would tell the client

**The short version:** what you have supports *diagnosis* — naming and counting defects
consistently, and testing which mixes are associated with which failures. It does not
support *prediction*, because prediction needs variables recorded before the pour, and
every variable you have is either fixed on paper (the mix design) or recorded after the
concrete has already failed (the photos). That gap is fixable, and cheaply.

## What can be built now

1. **A defect classifier over the photo set.** ~120 captioned images across three
   projects is too few to train from scratch, but enough to fine-tune a pretrained
   vision backbone (ResNet-50 or equivalent) into a defect-type classifier —
   cracking, honeycombing, spalling, scaling, cold joints. Expect useful top-1
   accuracy on the common classes and unreliable performance on any class with under
   ~15 examples, reported per class rather than as one headline number. Value: QA
   captions become consistent across engineers and sites, and the backlog becomes
   searchable. This is the foundation everything else needs, because today "defect
   type" means whatever the engineer on duty wrote.
2. **A mix-to-defect association table.** Three mix designs is three data points at
   the level that matters, so this is descriptive statistics with wide confidence
   intervals, not a model: defect rate per m³ by mix, broken down by defect type,
   with the uncertainty stated plainly. It is worth doing because it frames the
   right hypotheses (e.g. "the higher w/c-ratio mix shows 3× the plastic shrinkage
   cracking") and tells you which variable to instrument first.
3. **A structured intake pipeline.** Photo → defect type → pour ID → mix → date →
   location, stored once, so that within two or three projects you have a dataset
   that *can* be modelled. Most of the value of this engagement is that the fourth
   project arrives already instrumented.

## What cannot be built, and why

**"Predict defects before they happen" is not reachable with this data.** Three reasons,
in order of severity:

- **No leading indicators.** A mix design is an intention, not an observation. What
  determines whether a pour cracks is what happened between batching and curing:
  ambient temperature and wind, transit time, water added on site, placement rate,
  vibration, finishing timing, curing regime. None of that is in the dataset, so a
  model would be asked to predict outcomes from variables that do not vary within a
  project. It would "learn" project identity and score well in cross-validation — a
  result I would not put in front of a site team.
- **No negative examples.** You have sent 30–50 photos *of defects* per project.
  There are no photos of sound concrete, so a classifier cannot learn a decision
  boundary between "defective" and "fine", and there is no denominator: 40 defect
  photos out of 200 pours and 40 out of 2,000 are different worlds, and nothing in
  the pack distinguishes them.
- **n = 3 at the unit of analysis.** Individual photos are not independent samples of
  mix performance; the pour is. Three mixes cannot separate mix effects from crew,
  season, geometry or site effects, so any "the mix caused this" claim is confounded.

What I would say on the call: *"We can make your defect records consistent and tell you
which mixes are associated with which failures — that is real value and we can start
now. We cannot forecast a defect before the pour, because nothing you have measures the
conditions that actually cause them. Give us three specific additions and forecasting
becomes a genuine engineering problem rather than a guess."*

## The three data asks

1. **Pour-level join keys with photo metadata** — a pour/batch ID on every photo, plus
   timestamp, location on the structure, element type, and the total number of pours
   per project (defective and sound alike).
   *Unlocks:* the denominator and the causal link. Defect *rate* instead of defect
   *count*, mix attributed to the specific element that failed, and — critically —
   sound pours as negative examples. Without this the other two asks are unusable.

2. **Batch-to-placement process logs and site conditions** — batch time, truck
   departure and arrival, placement and finishing times, water or admixture added on
   site, slump tests, ambient temperature, humidity and wind at placement.
   *Unlocks:* actual prediction. These are the variables measured *before* the concrete
   sets, so a model on them produces a pre-pour or in-pour risk score — "this pour is
   in the top decile of cold-joint risk given a 95-minute transit at 34 °C" — which is
   actionable while a decision can still be made. This is where the forecasting
   capability comes from; the mix design is a covariate, not the driver.

3. **Curing and outcome records per pour** — in-situ temperature/maturity sensor traces
   or curing method and duration, plus cylinder-break strengths and the QA sign-off for
   every pour, including the ones that passed.
   *Unlocks:* labels worth predicting and the mass-pour failure mode. Strength and
   maturity give a graded outcome rather than a binary "someone photographed it", so
   the model can be validated against something objective; thermal traces make
   core-to-surface differential cracking predictable, which photos alone can only
   confirm after the fact.

**Sequencing:** ask 1 is a data-hygiene change and can start on the next pour; asks 2
and 3 need instrumentation. I would scope a diagnostic phase on asks 1 and 3 (four to
six weeks, classifier plus association table plus reporting), and gate the predictive
phase on two full projects of ask 2. I would rather tell you at week six that the
signal is not there than build a dashboard that quietly predicts the season.
