# SNAIC AI Program — Project CLAUDE.md

## Environment

**Interpreter**: `/opt/homebrew/anaconda3/bin/python` (conda `base`, Python 3.12.7)
⚠️ `python3` on PATH resolves to an empty Homebrew install — always use the anaconda one.

```bash
/opt/homebrew/anaconda3/bin/pip install -r requirements.txt
```

## Data

Location: `Week 1/Wk1_Capstone/data/`

| File                | Size  | Notes                                    |
|---------------------|-------|------------------------------------------|
| `train.csv`         | 77 MB | 594,194 rows × 21 cols, target = `Churn` |
| `test.csv`          | 32 MB | 254,656 rows × 20 cols (no `Churn`)      |
| `sample_submission.csv` | 2 MB | `id,Churn` (0/1 integers)            |

- Target: `Churn` (Yes/No in train → map to 1/0)
- Class split: No ≈ 77.5%, Yes ≈ 22.5% (moderately imbalanced)
- No missing values anywhere (confirmed)
- `SeniorCitizen` is already 0/1; all other categoricals are strings

## Running notebooks

```bash
# Fast iteration (30k stratified sample — use during fix/dev loops)
/opt/homebrew/anaconda3/bin/python run_nb.py "Week 1/Wk1_Capstone/eda_randomforest.ipynb" --sample 30000

# Full data run (final only — takes longer with 594k rows)
/opt/homebrew/anaconda3/bin/python run_nb.py "Week 1/Wk1_Capstone/eda_randomforest.ipynb"
```

`run_nb.py` prints ONLY the failing cell index + source + traceback tail (token-cheap).
Writes `*_executed.ipynb` (plots + outputs inspectable there).
Sets `CAPSTONE_SAMPLE` env var; notebooks read it to down-sample for fast iteration.

## Fix-loop protocol (autonomous)

```
1. run_nb.py --sample 30000
2. Read failing cell index + traceback from output
3. NotebookEdit to fix that cell
4. Re-run step 1
5. Repeat until exit 0
6. Final: run_nb.py (no --sample) for clean full-data run
```

## Capstone brief constraints (MUST respect in any fix or rebuild)

- **No DL / LLMs as the primary predictive component** (allowed for feature engineering only)
- Classical ensembles (Random Forest, Gradient Boosting, stacking) are allowed
- Hyperparameter tuning: **≤ 50 total iterations**, **~3 params per model**, with justification
- **Hold-out test set evaluated ONCE** — all model selection/tuning via CV on train only
- All transforms + estimator must be in a formal **`sklearn.pipeline.Pipeline`** (prevents leakage)
- Primary deliverable: **`groupX.pdf`** ≤ 12 slides; Slide 1 must include code repo link
- Deadline: **2026-06-19 15:00**
- `RANDOM_STATE = 42` throughout
- Required structure mirrors `Wine_Quality_Assignment.ipynb` §1–5 = Parts A–E:
  - Part A: Pipeline Engineering
  - Part B: Champion Model Selection (2–3 algorithmic families, 5-fold CV, mean±std)
  - Part C: Controlled Ablations (≤ 4 experiments, Ablation Log table)
  - Part D: Mechanical Failure Analysis (5–10 high-confidence FP/FN, explanation, proposed fix)
  - Part E: Business Decision Making (threshold shift direction + justification, executive summary)

## Known-bug catalogue (eda_randomforest.ipynb — NOT fixed in harness round)

These bugs are catalogued for the next fix round. The harness will surface them.

| Cell | Bug | Detail |
|------|-----|--------|
| 22 | `NameError: model` | `joblib.dump(model, …)` — trained object is named `baseline_model` |
| 22 | Colab path | `file_name` points to `/content/drive/…` — will fail locally |
| 38 | `NameError: acc` | `print(f"Accuracy: {acc:.4f}")` — `acc` never defined in this scope |
| 30/37 | Wrong AUC | `roc_auc_score` called on predicted labels not probabilities → not true AUC |
| 17+25 | Variable shadowing | `X_train/X_test/y_train/y_test` and `preprocessor` overwritten across sections; baseline (70/15/15) and comparison (80/20+SMOTE+chi2) results are non-comparable |
| 19 | Missing `()` | `baseline_model.get_feature_names_out` — missing call parens, prints bound method |
| — | No test inference | `test.csv` never loaded; no `submission.csv` generated |
| — | No threshold tuning | Part E entirely missing |

## Target rebuild structure (next rounds)

```
§1 Data Prep + Pipeline  →  Part A
§2 Champion Selection    →  Part B  (3 model families, 5-fold CV, metric = ROC-AUC primary)
§3 Ablations             →  Part C  (≤4 experiments, Ablation Log table)
§4 Failure Analysis      →  Part D  (5–10 high-confidence FP/FN)
§5 Business Decisions    →  Part E  (threshold shift + executive summary + submission.csv)
```
