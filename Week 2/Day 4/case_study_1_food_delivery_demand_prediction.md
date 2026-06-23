# Case Study 1: Food Delivery Demand Prediction System

## Interview Question

A food delivery company wants to predict the number of food orders in each area for the next 1-2 hours.

The prediction will be used to plan rider allocation, restaurant preparation, and promotion decisions.

Design an end-to-end MLOps system for this use case.

## Short Interview Answer

I would design this as a near-real-time demand forecasting system. The system needs reliable data ingestion, a feature store for consistent online and offline features, model training with experiment tracking, a model registry for controlled promotion, online or batch prediction for each area, monitoring for data drift and prediction quality, and a retraining workflow.

The key challenge is that food delivery demand changes quickly due to time of day, weather, holidays, events, promotions, restaurant availability, and rider supply. So I would treat this as a time-sensitive forecasting problem, not just a static regression model.

## 1. Business Objective

The model predicts:

```text
number_of_orders_next_1h or number_of_orders_next_2h
```

for each:

```text
area + prediction_time
```

The output will support:

- rider allocation;
- restaurant preparation;
- surge pricing or promotion decisions;
- operational planning.

The business metric is not only model accuracy. We care about operational impact:

- fewer late deliveries;
- lower rider idle time;
- better restaurant preparation;
- fewer cancelled orders;
- better promotion ROI.

## 2. Data Sources

Important data sources:

| Data source | Examples |
|---|---|
| Order history | order timestamp, area, restaurant, cuisine, basket size |
| Restaurant data | open/closed status, preparation capacity, menu availability |
| Rider supply | active riders per area, rider acceptance rate |
| Weather | rain, temperature, severe weather alerts |
| Calendar | day of week, public holiday, school holiday, payday |
| Promotions | discount campaign, free delivery, restaurant-specific promo |
| Local events | concerts, sports events, office district events |
| Traffic | congestion level, travel time |
| App behavior | search volume, add-to-cart count, session count |

Some features are available immediately. Some are delayed. The system must record data freshness and avoid using future information during training.

## 3. Feature Design

Useful features:

| Feature group | Examples |
|---|---|
| Time features | hour, day_of_week, is_weekend, holiday flag |
| Lag demand | orders last 15 min, 30 min, 1h, same hour yesterday |
| Rolling demand | rolling average, rolling max, trend over last 2h |
| Weather features | is_raining, rain intensity, temperature |
| Promotion features | active promotion flag, discount percentage |
| Restaurant supply | open restaurants, average prep time |
| Rider supply | active riders, rider shortage flag |
| Area features | residential/business area, mall/office zone |

I would use a feature store so that the training pipeline and online prediction service use the same feature definitions.

Example feature service:

```text
food_delivery_demand_model_v1
```

Example entity:

```text
area_id
```

Example feature views:

```text
area_order_features_v1
area_weather_features_v1
area_promotion_features_v1
rider_supply_features_v1
```

## 4. Model Choice

I would start simple, then improve.

Baseline:

```text
historical average by area + hour + day_of_week
```

First ML model:

```text
LightGBM / XGBoost regression
```

Why:

- strong tabular performance;
- handles nonlinear effects;
- easier to explain than deep learning;
- fast enough for frequent retraining.

Later options:

- quantile regression for prediction intervals;
- time-series models per area;
- global forecasting model across all areas;
- deep learning sequence models if scale and data justify it.

The model should output both:

```text
expected demand
uncertainty range
```

because rider allocation should account for risk, not only point estimates.

## 5. Training Pipeline

Training workflow:

```text
raw data
-> validation
-> feature generation
-> point-in-time training dataset
-> train model
-> evaluate
-> log to MLflow
-> register candidate model
```

Important MLflow records:

- data version;
- feature service version;
- model type;
- hyperparameters;
- metrics by area and time period;
- training window;
- model artifact.

Important evaluation slices:

- peak vs non-peak hours;
- rainy vs non-rainy periods;
- high-demand vs low-demand areas;
- promotion vs no-promotion periods;
- weekdays vs weekends;
- new areas vs mature areas.

Do not only report one global MAE. A model can look good globally but fail in peak-hour areas.

## 6. Serving Design

Prediction frequency:

```text
every 5-15 minutes
```

Prediction granularity:

```text
area_id x next_1h or next_2h
```

Serving options:

| Option | Use case |
|---|---|
| Batch prediction every 15 min | enough for rider planning dashboard |
| Online API | if dispatch system needs on-demand predictions |

For this case, I would likely use scheduled batch prediction because every area needs a forecast at regular intervals.

Serving flow:

```text
scheduler triggers prediction job
-> read area list
-> fetch online features from feature store
-> load champion model from registry
-> generate forecasts
-> write predictions to prediction table
-> operations dashboard reads prediction table
```

The prediction record should include:

- prediction timestamp;
- target time window;
- area_id;
- predicted orders;
- prediction interval;
- model name and version;
- feature service version;
- feature freshness;
- input feature snapshot.

## 7. Monitoring

I would monitor four layers.

### Data Health

- missing values;
- delayed data;
- duplicate events;
- invalid timestamps;
- sudden drop in order event volume;
- feature freshness.

Example alert:

```text
Weather feature for area_id=123 is older than 60 minutes.
```

### Feature Drift

Compare current feature distribution with training distribution.

Examples:

- average orders in last 30 min suddenly much higher;
- rain feature distribution changed due to monsoon season;
- promotion flag much more common than in training data;
- active rider count lower than normal.

Useful metrics:

- PSI;
- KL divergence;
- Wasserstein distance;
- z-score against historical baseline;
- feature missing rate.

### Prediction Drift

Monitor prediction distribution:

- average predicted demand by area;
- percentage of very high forecasts;
- prediction variance;
- prediction distribution by hour.

Example:

```text
The model suddenly predicts near-zero demand for all CBD areas at lunch time.
```

This may indicate input data failure, not true demand collapse.

### Performance Monitoring

After actual orders arrive, compare:

```text
predicted orders vs actual orders
```

Metrics:

- MAE;
- RMSE;
- MAPE or WAPE;
- bias: average prediction - actual;
- underprediction rate;
- overprediction rate;
- metrics by area/hour/weather/promotion.

For operations, bias is very important:

```text
underprediction -> not enough riders
overprediction -> too many idle riders
```

## 8. Online Drift Scenarios And Responses

### Drift 1: Demand Pattern Changes

Example:

```text
More people start ordering late at night after a new campaign.
```

Signal:

- actual orders consistently higher than predictions at night;
- error concentrated in specific hours.

Response:

- check if promotion/calendar features capture the new behavior;
- retrain with recent data;
- add campaign features;
- consider time-decay weighting so recent data matters more.

### Drift 2: Weather Relationship Changes

Example:

```text
Rain used to increase orders, but during severe storms riders become unavailable and orders drop.
```

Signal:

- high error during rainy periods;
- rain feature distribution is normal, but model error changes.

Response:

- add rider supply and severe weather features;
- evaluate rainy-day slice separately;
- retrain with more extreme-weather examples;
- possibly build separate severe-weather logic.

### Drift 3: Promotion Drift

Example:

```text
Marketing starts using aggressive discounts that were not in training data.
```

Signal:

- promotion flag or discount level distribution shifts;
- underprediction during campaigns.

Response:

- add promotion intensity features;
- require marketing campaign data before serving predictions;
- retrain with campaign data;
- run promotion-specific validation.

### Drift 4: Area-Level Drift

Example:

```text
A new mall opens in an area.
```

Signal:

- persistent error in one area;
- local demand no longer matches historical pattern.

Response:

- monitor per-area bias;
- add local event or POI features;
- allow area-specific calibration;
- retrain more frequently for high-change areas.

### Drift 5: Data Pipeline Drift

Example:

```text
Order events arrive 30 minutes late.
```

Signal:

- recent order count features drop suddenly;
- actual business orders still normal;
- feature freshness alert fires.

Response:

- fail open to baseline forecast;
- alert data engineering;
- mark predictions as degraded;
- avoid retraining on corrupted data.

## 9. Retraining Strategy

I would use both scheduled and triggered retraining.

Scheduled:

```text
daily or weekly retraining
```

Triggered:

```text
if rolling MAE or bias exceeds threshold for N hours
if feature drift is severe
if major campaign or product change starts
```

Retraining must include:

- data validation;
- point-in-time feature correctness;
- backtesting on recent weeks;
- slice evaluation;
- comparison against champion;
- approval gate before promotion.

## 10. Safe Deployment

I would not immediately replace the champion model.

Deployment strategy:

- register model as candidate;
- run offline validation;
- run shadow deployment;
- compare candidate vs champion on live traffic;
- canary release to a few areas;
- monitor operational metrics;
- promote to champion if stable;
- keep rollback path.

Rollback should be easy:

```text
move champion alias back to previous model version
```

## 11. Strong Interview Answer Structure

If asked in an interview, answer in this order:

1. Clarify prediction target and decision use.
2. Describe data sources and feature store.
3. Explain training and experiment tracking.
4. Explain serving pattern.
5. Explain monitoring by data, feature, prediction, and performance.
6. Discuss drift scenarios.
7. Explain retraining and safe deployment.

## 12. Common Follow-Up Questions And Answers

### Q1. Why not just retrain every day?

Daily retraining helps, but it does not solve data quality issues or sudden business changes. If the recent data pipeline is broken, daily retraining can make the model worse. I would combine scheduled retraining with monitoring gates and data validation.

### Q2. What is the most dangerous drift here?

For food delivery, the most dangerous drift is local and time-specific drift: for example, lunch demand in CBD areas or rainy evening demand. Global metrics may hide this, so I would monitor slices by area, hour, weather, and promotion.

### Q3. What if labels arrive late?

Actual order counts for the next 1-2 hours arrive after the forecast window. That is acceptable. I would separate real-time feature monitoring from delayed performance monitoring. Performance metrics update once actuals are available.

### Q4. How do you handle a feature pipeline failure?

The prediction service should detect stale or missing features. It can fall back to a baseline model, such as historical average by area and hour, and mark predictions as degraded. It should not silently serve bad predictions.

### Q5. Why use a feature store?

Because the same feature definitions must be used in training and serving. Without a feature store, the training pipeline and online API may calculate lag windows, weather joins, or promotion flags differently.

### Q6. What metric would you optimize?

I would start with WAPE or MAE because they are easy to interpret for order counts. But I would also monitor bias because underprediction and overprediction have different operational costs.

### Q7. How would you explain this system to operations?

I would say: every 15 minutes the system predicts demand for each area for the next 1-2 hours. It uses recent orders, weather, promotions, restaurant availability, and rider supply. If data is stale or the model becomes unreliable, the system alerts us and can fall back to a safer baseline.

