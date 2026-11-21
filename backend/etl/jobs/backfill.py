from etl.jobs import aggregate_dws, aggregate_themes


def backfill(start_date, end_date, *, dataset_id=None, batch_date=None, env="dev"):
    return aggregate_dws.rerun(
        (start_date, end_date), dataset_id=dataset_id, batch_date=batch_date,
        env=env, themes=tuple(aggregate_themes.TABLES),
    )
