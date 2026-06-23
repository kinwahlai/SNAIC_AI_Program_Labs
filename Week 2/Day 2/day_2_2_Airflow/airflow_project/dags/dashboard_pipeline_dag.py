from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator

from jobs.taxi_job import (
    clean_taxi_data,
    fetch_taxi_data,
    join_taxi_to_planning_area,
    load_taxi_to_duckdb,
)
from jobs.rainfall_job import (
    clean_rainfall_data,
    fetch_rainfall_data,
    join_rainfall_to_planning_area,
    load_rainfall_to_duckdb,
)


# `with DAG(...) as dag:` is a Python context manager.
# In Airflow, it means: "all tasks created inside this block belong to this DAG."
# This keeps the DAG definition readable and avoids passing `dag=dag` to every task.
with DAG(
    # dag_id is the unique name shown in the Airflow Web UI.
    dag_id="dashboard_data_pipeline",
    # A short human-readable explanation for the DAG page.
    description="Fetch, clean, join, and load taxi/rainfall data for the dashboard",
    # Airflow will not create scheduled runs before this date.
    start_date=datetime(2026, 1, 1),
    # Run this DAG every 2 minutes while it is turned on.
    schedule=timedelta(minutes=2),
    # catchup=False means: do not automatically create old missing runs
    # between start_date and today. This keeps the classroom demo simple.
    catchup=False,
    # Only one DAG run can be active at a time. This avoids two runs writing
    # to the same local DuckDB file at the same time.
    max_active_runs=1,
    # Tags make DAGs easier to find and filter in the Airflow Web UI.
    tags=["data-engineering", "dashboard"],
) as dag:

    # Each PythonOperator turns one Python function into one visible Airflow task.
    fetch_taxi = PythonOperator(
        # task_id is the task name shown in the Airflow graph and logs.
        task_id="fetch_taxi_api",
        # python_callable is the Python function Airflow will execute.
        python_callable=fetch_taxi_data,
        # API calls can fail temporarily, so this task is allowed to retry.
        retries=2,
        # The first retry waits 30 seconds.
        retry_delay=timedelta(seconds=30),
        # Exponential backoff increases the wait between retries.
        # This is common when calling external APIs because repeated immediate
        # retries can make rate limits or outages worse.
        retry_exponential_backoff=True,
        # Cap the retry delay so a bad API does not make the demo wait forever.
        max_retry_delay=timedelta(minutes=10),
    )

    clean_taxi = PythonOperator(
        task_id="clean_taxi_points",
        python_callable=clean_taxi_data,
        # op_args passes values into the Python function.
        # fetch_taxi.output is Airflow's XCom output from the previous task.
        op_args=[fetch_taxi.output],
    )

    join_taxi_area = PythonOperator(
        task_id="join_taxi_to_planning_area",
        python_callable=join_taxi_to_planning_area,
        # This task receives the clean taxi CSV path from clean_taxi.
        op_args=[clean_taxi.output],
    )

    load_taxi = PythonOperator(
        task_id="load_taxi_counts_to_duckdb",
        python_callable=load_taxi_to_duckdb,
        # This task receives the joined taxi CSV path from join_taxi_area.
        op_args=[join_taxi_area.output],
        # pool limits concurrent tasks that use the same shared resource.
        # Here it protects the local DuckDB file from simultaneous writes.
        pool="data_ingestion_pool",
    )

    fetch_rainfall = PythonOperator(
        task_id="fetch_current_rainfall_api",
        python_callable=fetch_rainfall_data,
        # Rainfall is also an external API call, so use the same retry pattern.
        retries=2,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=10),
    )

    clean_rainfall = PythonOperator(
        task_id="clean_current_rainfall",
        python_callable=clean_rainfall_data,
        # This task receives the raw rainfall JSON path from fetch_rainfall.
        op_args=[fetch_rainfall.output],
    )

    join_rainfall_area = PythonOperator(
        task_id="join_rainfall_to_planning_area",
        python_callable=join_rainfall_to_planning_area,
        # This task receives the clean rainfall station CSV path from clean_rainfall.
        op_args=[clean_rainfall.output],
    )

    load_rainfall = PythonOperator(
        task_id="load_current_rainfall_to_duckdb",
        python_callable=load_rainfall_to_duckdb,
        # This task receives the rainfall area CSV path from join_rainfall_area.
        op_args=[join_rainfall_area.output],
        # Same pool as load_taxi: only one DuckDB-writing task can run at once.
        pool="data_ingestion_pool",
    )

    # `>>` defines task order. The right task waits for the left task to finish.
    fetch_taxi >> clean_taxi >> join_taxi_area >> load_taxi
    fetch_rainfall >> clean_rainfall >> join_rainfall_area >> load_rainfall
