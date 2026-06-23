# Case Study 2: Bank Loan Default Model Monitoring And Safe Update

## Interview Question

A bank uses a machine learning model to predict whether a loan applicant is likely to default.

The model was trained on historical loan application and repayment data.

It is now deployed to support loan approval decisions.

After several months, the business team reports that the model's risk scores no longer seem reliable.

Design an MLOps approach to monitor, debug, retrain, and safely update this model.

## Short Interview Answer

I would treat this as a high-risk regulated ML system. The first step is not to immediately retrain, but to investigate whether the issue comes from data quality, feature drift, population drift, concept drift, label delay, calibration drift, policy changes, or serving bugs.

The system needs monitoring at multiple levels: data quality, feature distribution, prediction distribution, model calibration, business outcomes, fairness, and operational logs. For updates, I would use strict validation, model governance, challenger testing, approval workflow, canary or shadow deployment, and rollback.

## 1. Business Objective

The model predicts:

```text
probability_of_default
```

for each loan applicant.

The model output may be used for:

- approve / reject recommendation;
- interest rate decision;
- credit limit decision;
- manual review prioritization;
- risk portfolio management.

This is a high-stakes domain. The system must consider:

- accuracy;
- calibration;
- fairness;
- explainability;
- auditability;
- regulatory compliance;
- safe rollback.

## 2. Why The Model May Become Unreliable

When the business says "risk scores no longer seem reliable", possible causes include:

| Cause | Example |
|---|---|
| Data quality issue | income field suddenly missing or scaled differently |
| Feature drift | applicant income distribution changes |
| Population drift | new customer segment applies for loans |
| Concept drift | default behavior changes due to economic conditions |
| Policy drift | bank changed approval rules, so observed borrowers differ |
| Label delay | defaults are only known months later |
| Calibration drift | predicted 10% risk no longer means 10% default rate |
| Serving bug | production feature transformation differs from training |
| External shock | recession, interest rate increase, unemployment spike |
| Fraud pattern change | applicants manipulate inputs or new fraud behavior appears |

Do not jump directly to retraining. First identify which failure mode is happening.

## 3. End-To-End MLOps Architecture

The system should include:

```text
raw application data
-> data validation
-> feature store
-> model training pipeline
-> experiment tracking
-> model registry
-> approval workflow
-> online/batch scoring service
-> monitoring and alerting
-> retraining pipeline
-> safe deployment and rollback
```

Important stored records:

- applicant ID or anonymized application ID;
- application timestamp;
- model version;
- feature service version;
- feature values used at decision time;
- risk score;
- decision threshold;
- final business decision;
- human override flag;
- later repayment/default outcome;
- explanation reason codes.

These records are critical for debugging months later.

## 4. Feature Store Design

A feature store is useful because loan features may be used in both training and production scoring.

Entities:

```text
applicant_id
application_id
customer_id
```

Feature groups:

| Feature group | Examples |
|---|---|
| Application features | requested amount, term, product type |
| Customer profile | age band, employment length, income |
| Credit history | credit score, past delinquencies, utilization |
| Banking behavior | account balance trend, salary deposits |
| Macro features | interest rate, unemployment rate, inflation |
| Policy features | current underwriting policy version |

The feature service should be logged with the model:

```text
loan_default_model_v3
feature_service = loan_default_features_v3
```

This helps ensure that the production model receives the same feature contract it was trained with.

## 5. Monitoring Layers

### Data Quality Monitoring

Monitor:

- missing rate by feature;
- invalid values;
- schema changes;
- category changes;
- outliers;
- stale data;
- failed joins;
- unexpected zero values.

Examples:

```text
income is null for 40% of applications
credit_score values shift from 300-850 to 0-1
employment_status has a new category: gig_worker
```

These issues can make risk scores unreliable even if the model itself is unchanged.

### Feature Drift Monitoring

Compare current applicants with training applicants.

Features to monitor:

- income;
- loan amount;
- debt-to-income ratio;
- credit score;
- employment length;
- product type;
- application channel;
- region;
- macroeconomic variables.

Useful drift metrics:

- PSI;
- Jensen-Shannon divergence;
- Wasserstein distance;
- missing rate change;
- category distribution shift.

Important: monitor by segment, not only globally.

Segments:

- product type;
- region;
- application channel;
- income band;
- credit score band;
- new vs existing customers.

### Prediction Drift Monitoring

Monitor the model output:

- average risk score;
- risk score distribution;
- approval rate by score band;
- percentage of high-risk applicants;
- threshold crossing rate.

Example alert:

```text
Average predicted default probability drops from 8% to 2% while applicant mix did not improve.
```

This may indicate feature pipeline failure or calibration drift.

### Performance Monitoring

Loan default labels are delayed. We may not know true default for 3, 6, or 12 months.

When labels arrive, monitor:

- AUC;
- PR-AUC;
- KS statistic;
- Brier score;
- log loss;
- calibration by score band;
- default rate by decile;
- approval bad rate;
- false negative rate among approved loans.

For banking, calibration is extremely important:

```text
Among applicants scored around 10% risk, about 10% should default.
```

If calibration breaks, business decisions become unreliable even if ranking metrics are acceptable.

### Fairness Monitoring

Depending on legal and company requirements, monitor fairness across protected or sensitive groups.

Possible checks:

- approval rate parity;
- false negative / false positive rate differences;
- calibration by group;
- adverse impact ratio;
- reason code distribution.

This must be handled carefully with legal and compliance teams.

## 6. Debugging Workflow

When business reports unreliable scores, I would debug in this order.

### Step 1: Confirm The Symptom

Ask:

- Which segment is unreliable?
- Which product?
- Which region?
- Which time period?
- Are scores too high, too low, or poorly ranked?
- Is this based on actual defaults or business intuition?

Create a dashboard:

```text
risk score distribution over time
approval rate over time
default rate by score band
score distribution by segment
```

### Step 2: Check Data And Feature Pipeline

Verify:

- schema unchanged;
- feature freshness;
- missing values;
- joins still working;
- transformations match training;
- categorical encodings still valid;
- feature store online/offline consistency.

Example:

```text
Production debt_to_income = 0 for many applicants because monthly_debt join failed.
```

This is not a model drift issue. It is a data pipeline issue.

### Step 3: Check Population Drift

Compare recent applications with training data.

Example:

```text
The bank launched a new online campaign and attracted younger thin-file applicants.
```

The model may be seeing applicants unlike the training population.

### Step 4: Check Concept Drift

Concept drift means the relationship between features and default changes.

Example:

```text
Due to rising interest rates, applicants with the same debt-to-income ratio now default more often.
```

This requires retraining or adding macroeconomic features.

### Step 5: Check Calibration

Create calibration table:

| Score band | Predicted default | Actual default |
|---|---:|---:|
| 0-5% | 3% | 6% |
| 5-10% | 8% | 14% |
| 10-20% | 15% | 25% |

If actual default is higher than predicted across bands, the model is underestimating risk.

### Step 6: Check Policy And Feedback Effects

In lending, labels are biased by decisions.

We only observe repayment outcomes for approved applicants. Rejected applicants usually have no default label.

If the bank changed approval policy, the observed label population changes.

This can create misleading monitoring metrics.

## 7. Online Drift Scenarios And Responses

### Drift 1: Applicant Population Drift

Example:

```text
The bank starts receiving more applications from gig workers.
```

Signal:

- employment_type distribution changes;
- model has higher error for this segment;
- more applications fall into missing/unknown categories.

Response:

- add or improve gig-worker features;
- collect more labels;
- create segment-specific validation;
- consider separate model or calibration for this segment;
- possibly route uncertain cases to manual review.

### Drift 2: Macroeconomic Concept Drift

Example:

```text
Interest rates rise and default risk increases for the same borrower profile.
```

Signal:

- calibration deteriorates;
- actual default rate rises within each score band;
- broad underprediction across many segments.

Response:

- add macroeconomic features;
- retrain with recent data;
- recalibrate probabilities;
- temporarily adjust decision thresholds;
- increase monitoring frequency.

### Drift 3: Data Pipeline Drift

Example:

```text
Credit bureau field changes format.
```

Signal:

- credit_score distribution shifts abruptly;
- missing or default values increase;
- score distribution changes on the same day as pipeline release.

Response:

- rollback feature pipeline change;
- backfill correct features;
- disable affected model version if needed;
- use fallback decision policy;
- do not retrain until data is corrected.

### Drift 4: Calibration Drift

Example:

```text
Applicants predicted at 5% risk now default at 12%.
```

Signal:

- Brier score worsens;
- calibration curve shifts;
- score bands no longer match observed default rates.

Response:

- recalibrate using Platt scaling or isotonic regression;
- update threshold policy;
- retrain if ranking also worsened;
- validate calibration by segment.

### Drift 5: Fraud Or Adversarial Drift

Example:

```text
Applicants learn which fields improve approval chance and manipulate inputs.
```

Signal:

- suspicious feature patterns increase;
- high approval but high default in a new pattern;
- sudden concentration of applications with similar values.

Response:

- add fraud detection features;
- monitor suspicious clusters;
- add manual review rules;
- retrain with fraud labels;
- collaborate with fraud risk team.

### Drift 6: Policy Drift

Example:

```text
The bank changed approval thresholds, so only safer applicants get approved.
```

Signal:

- observed default rate changes;
- label distribution changes;
- performance metrics on approved applicants no longer represent all applicants.

Response:

- log policy version with each decision;
- evaluate metrics by policy period;
- account for selection bias;
- avoid comparing old and new periods naively.

## 8. Retraining Approach

Retraining should not be automatic without controls because this is a regulated high-stakes model.

Retraining pipeline:

```text
collect recent labeled data
-> validate data quality
-> build point-in-time training set
-> train challenger model
-> evaluate globally and by segment
-> check calibration
-> check fairness
-> produce explainability report
-> compare with champion
-> model risk approval
-> shadow/canary deployment
-> promote if safe
```

Important training considerations:

- labels are delayed;
- rejected applicants may not have labels;
- recent data may be biased by current policy;
- avoid leakage from future repayment behavior;
- keep model explainable enough for audit.

## 9. Safe Model Update

I would use a controlled promotion process:

1. Train challenger model.
2. Compare with champion on a holdout set.
3. Evaluate important slices.
4. Check calibration and fairness.
5. Run shadow mode on live applications.
6. Compare challenger score vs champion score.
7. Review with risk/compliance/business.
8. Canary release to a small percentage or low-risk segment.
9. Monitor closely.
10. Promote to champion only if safe.

Rollback:

```text
move champion alias back to previous model version
```

Also keep threshold configuration versioned separately from model version.

## 10. Governance And Audit

Because this is lending, the system should keep:

- model version;
- feature version;
- training data period;
- approval threshold;
- reason codes;
- human override;
- data used for each decision;
- monitoring reports;
- approval sign-off.

For each prediction, the bank should be able to answer:

```text
Why did this applicant receive this score?
Which model version made it?
Which features were used?
What threshold and policy were active?
Was there a human override?
```

## 11. Strong Interview Answer Structure

If asked in an interview, answer in this order:

1. State that this is high-risk and regulated.
2. Do not jump to retraining; debug data, drift, calibration, and policy first.
3. Describe monitoring layers.
4. Discuss delayed labels and selection bias.
5. Explain retraining with governance.
6. Explain safe deployment and rollback.
7. Mention fairness and auditability.

## 12. Common Follow-Up Questions And Answers

### Q1. If the model is unreliable, should we immediately retrain?

No. First determine whether the issue is data quality, serving bug, population drift, concept drift, calibration drift, or policy change. Retraining on bad or biased data can make the system worse.

### Q2. What is the hardest part of monitoring loan default models?

Labels are delayed and biased. We only know default after months, and usually only for approved applicants. This makes real-time performance monitoring harder than in domains where labels arrive quickly.

### Q3. What drift is most dangerous here?

Calibration drift is very dangerous because business decisions depend on the probability meaning. If a 5% risk score now corresponds to 12% actual default, approval and pricing decisions become unsafe.

### Q4. How do you monitor before default labels arrive?

Use proxy monitoring:

- data quality;
- feature drift;
- score distribution;
- approval rate;
- early repayment signals;
- delinquency early indicators;
- manual review feedback.

But final performance still requires delayed outcome labels.

### Q5. How do you handle selection bias?

The model only observes repayment outcomes for approved loans. I would log decision policy, evaluate on approved population carefully, use reject inference cautiously, and work with risk experts. I would not blindly treat missing rejected outcomes as non-default.

### Q6. What if the model is ranking applicants well but probabilities are wrong?

Then recalibration may be enough. I would check AUC/KS for ranking and Brier/calibration curve for probability quality. If ranking is still good but calibration is bad, use recalibration before full model replacement.

### Q7. What if fairness metrics worsen after retraining?

Do not promote the model automatically. Investigate affected groups, check data quality and feature changes, involve compliance/legal teams, and consider constraints, recalibration, threshold changes, or model redesign.

### Q8. What should be logged for every prediction?

At minimum:

- application ID;
- timestamp;
- model version;
- feature service version;
- input feature snapshot;
- risk score;
- decision threshold;
- final decision;
- reason codes;
- human override;
- later outcome label when available.

### Q9. How do you safely update the model?

Use champion/challenger. Validate offline, run shadow mode, compare scores, canary release, monitor, then promote. Keep rollback simple by moving the model registry alias back to the previous version.

### Q10. How would you explain this to the business team?

I would say: we will first check whether the model is wrong because applicants changed, the economy changed, data pipelines changed, or the model probabilities became miscalibrated. Then we will update the model only after validation, fairness checks, and controlled rollout.

