# Wk1 Capstone — Grading Rubric

| Criterion | Excellent | Good | Average | Fail to meet expectations |
|---|---|---|---|---|
| **Pipeline Engineering & Data Prep** | Pipeline objects are correctly implemented with no evidence of data leakage. Advanced transformations are well-justified. | Pipeline objects are used, but with minor inefficiencies in data transformation steps. | Procedural code is used, presenting minor leakage risks, or pipelines are implemented incorrectly. | Significant data leakage is present, or transformations are applied globally before train/test splits. |
| **Model Selection & Tuning Discipline** | Cross-validation is appropriately used for comparison. The ablation log clearly connects hypotheses to controlled changes. Tuning limits are strictly observed. | Cross-validation is implemented. Ablations are logical, but hypotheses lack depth. | Cross-validation is poorly implemented or missing. Ablation experiments lack a clear systematic approach. | Grid searches exceed limits without justification, or the test set is used for iterative tuning. |
| **Failure Analysis** | Row-level data is extracted. Specific insights into model failure are provided based on feature interactions. Clear technical fixes are proposed. | Specific failure modes are identified, but the analysis relies heavily on aggregate metrics rather than row-level inspection. | Observations are general (e.g., "Recall is low") without a clear connection to the underlying data characteristics. | Failure analysis is missing or demonstrates a misunderstanding of the model's errors. |
| **Business Decision Reasoning** | Logically identifies the most costly error (e.g., FP vs. FN) and provides a highly convincing, context-specific justification for shifting the threshold or applying a safety margin. | Identifies the correct error to avoid and suggests a threshold shift, but the business justification is somewhat generic or lacks deep alignment with Stage 1. | Attempts to discuss business costs, but the logic for the threshold/margin shift is flawed, backward, or disconnected from the actual use case. | Retains the default 0.5 threshold without question, completely ignores the business context, or fails to address error costs entirely. |

---

## Self-evaluation worksheet

| Criterion | Rating (Excellent / Good / Average / Fail) | Evidence / notes | Fix before submission |
|---|---|---|---|
| Pipeline Engineering & Data Prep | **Excellent** | §1.3 `ImbPipeline` (ColumnTransformer→SMOTE→clf); SMOTE inside pipeline = train-fold only, no leakage; 80/20 stratified split, `X_test` locked until §4; chi² `SelectPercentile(50%)` justified | — |
| Model Selection & Tuning Discipline | **Good** | §2 `cross_validate` 3 families at defaults, 5-fold stratified, ROC-AUC+recall; §3 `GridSearchCV` champion-only ≤12 configs (≤50 cap justified), tune-subsample→clone+refit-full, test set never used for tuning | Rubric "Excellent" rewards *ablation log tying hypotheses→controlled changes*; GridSearch satisfies CV+limits but not hypothesis framing. Optional: add 1–2 explicit ablation hypotheses in §3 prose to lift toward Excellent |
| Failure Analysis | **Omitted (accepted team trade-off)** | Part D (row-level FP/FN, feature-interaction insight) intentionally dropped. `val_proba` from cross_val_predict exists in notebook if ever revisited | Known accepted gap — this criterion is forgone |
| Business Decision Reasoning | **Good** | §4 identifies FN as costliest error (lost LTV ≫ voucher cost), asymmetric cost table, threshold lowered 0.50→T at recall≥0.80 (justified), exec summary | Tie threshold rationale back to §1 EDA drivers (short tenure / month-to-month contract = highest-risk segments) for deeper Stage-1 alignment → Excellent |
