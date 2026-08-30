"""Суточные агрегаты одним SQL на разрез.

Считать это в Python значило бы вытащить всё сырьё в приложение ради
нескольких чисел. Запросы идемпотентны: UPSERT по ключу уникальности,
поэтому повторный прогон за то же окно ничего не ломает.
"""

from __future__ import annotations

from sqlalchemy import text

HARVEST_ROLLUP = text(
    """
    INSERT INTO metrics_harvest_daily (
        id, day, source, fetched, accepted, borderline, rejected, duplicates,
        avg_keyword_score, updated_at)
    SELECT gen_random_uuid(),
           (d.created_at AT TIME ZONE :tz)::date AS day,
           d.source,
           count(*),
           count(*) FILTER (WHERE d.decision = 'accepted'),
           count(*) FILTER (WHERE d.decision = 'borderline'),
           count(*) FILTER (WHERE d.decision = 'rejected'),
           count(*) FILTER (WHERE d.decision = 'duplicate'),
           avg(d.keyword_score),
           now()
      FROM harvest_decisions d
     WHERE (d.created_at AT TIME ZONE :tz)::date BETWEEN :day_from AND :day_to
     GROUP BY 2, 3
    ON CONFLICT (day, source) DO UPDATE SET
        fetched = EXCLUDED.fetched, accepted = EXCLUDED.accepted,
        borderline = EXCLUDED.borderline, rejected = EXCLUDED.rejected,
        duplicates = EXCLUDED.duplicates,
        avg_keyword_score = EXCLUDED.avg_keyword_score, updated_at = now()
    """
)

SUBSCRIBERS_ROLLUP = text(
    """
    INSERT INTO metrics_subscribers_daily (
        id, day, kind, registered, total, total_active, dau, wau, mau, updated_at)
    SELECT gen_random_uuid(), g.day, k.kind,
           (SELECT count(*) FROM subscribers s
             WHERE s.kind = k.kind
               AND (s.first_seen_at AT TIME ZONE :tz)::date = g.day),
           (SELECT count(*) FROM subscribers s
             WHERE s.kind = k.kind
               AND (s.first_seen_at AT TIME ZONE :tz)::date <= g.day),
           (SELECT count(*) FROM subscribers s
             WHERE s.kind = k.kind AND s.status = 'active'
               AND (s.first_seen_at AT TIME ZONE :tz)::date <= g.day),
           (SELECT count(DISTINCT a.subscriber_id) FROM subscriber_activity a
              JOIN subscribers s ON s.id = a.subscriber_id AND s.kind = k.kind
             WHERE (a.occurred_at AT TIME ZONE :tz)::date = g.day),
           (SELECT count(DISTINCT a.subscriber_id) FROM subscriber_activity a
              JOIN subscribers s ON s.id = a.subscriber_id AND s.kind = k.kind
             WHERE (a.occurred_at AT TIME ZONE :tz)::date
                   BETWEEN g.day - 6 AND g.day),
           (SELECT count(DISTINCT a.subscriber_id) FROM subscriber_activity a
              JOIN subscribers s ON s.id = a.subscriber_id AND s.kind = k.kind
             WHERE (a.occurred_at AT TIME ZONE :tz)::date
                   BETWEEN g.day - 29 AND g.day),
           now()
      -- generate_series по датам отдаёт timestamptz, а дальше идёт арифметика
      -- вида g.day - 6: без приведения к date это timestamptz минус integer.
      FROM (
           SELECT d::date AS day
             FROM generate_series(
                    CAST(:day_from AS date), CAST(:day_to AS date), '1 day'
                  ) AS d
      ) AS g
     CROSS JOIN (VALUES ('user'), ('group'), ('channel')) AS k(kind)
    ON CONFLICT (day, kind) DO UPDATE SET
        registered = EXCLUDED.registered, total = EXCLUDED.total,
        total_active = EXCLUDED.total_active, dau = EXCLUDED.dau,
        wau = EXCLUDED.wau, mau = EXCLUDED.mau, updated_at = now()
    """
)

DELIVERY_ROLLUP = text(
    """
    INSERT INTO metrics_delivery_daily (
        id, day, channel, jobs_created, jobs_sent, jobs_failed, messages_sent,
        messages_failed, recipients, avg_queue_seconds, p95_queue_seconds, updated_at)
    SELECT gen_random_uuid(),
           (j.created_at AT TIME ZONE :tz)::date AS day,
           j.channel,
           count(*),
           count(*) FILTER (WHERE j.status = 'sent'),
           count(*) FILTER (WHERE j.status = 'failed'),
           coalesce(sum(m.sent), 0),
           coalesce(sum(m.failed), 0),
           count(DISTINCT j.subscriber_id),
           avg(EXTRACT(EPOCH FROM (j.started_at - j.scheduled_at))),
           percentile_cont(0.95) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (j.started_at - j.scheduled_at))),
           now()
      FROM delivery_jobs j
      LEFT JOIN LATERAL (
           SELECT count(*) FILTER (WHERE status = 'sent') AS sent,
                  count(*) FILTER (WHERE status = 'failed') AS failed
             FROM delivery_messages WHERE delivery_job_id = j.id
      ) m ON true
     WHERE (j.created_at AT TIME ZONE :tz)::date BETWEEN :day_from AND :day_to
     GROUP BY 2, 3
    ON CONFLICT (day, channel) DO UPDATE SET
        jobs_created = EXCLUDED.jobs_created, jobs_sent = EXCLUDED.jobs_sent,
        jobs_failed = EXCLUDED.jobs_failed, messages_sent = EXCLUDED.messages_sent,
        messages_failed = EXCLUDED.messages_failed, recipients = EXCLUDED.recipients,
        avg_queue_seconds = EXCLUDED.avg_queue_seconds,
        p95_queue_seconds = EXCLUDED.p95_queue_seconds, updated_at = now()
    """
)

RETENTION_ROLLUP = text(
    """
    INSERT INTO metrics_retention (
        id, cohort_week, week_offset, kind, cohort_size, retained, retention_rate, updated_at)
    WITH cohorts AS (
        SELECT id, kind,
               date_trunc('week', first_seen_at AT TIME ZONE :tz)::date AS cohort_week
          FROM subscribers
         WHERE first_seen_at >= now() - make_interval(weeks => :weeks)
    ), sized AS (
        SELECT cohort_week, kind, count(*) AS cohort_size FROM cohorts GROUP BY 1, 2
    ), active AS (
        SELECT c.cohort_week, c.kind,
               -- Разность двух date в PostgreSQL — уже целое число дней,
               -- а не интервал, поэтому EXTRACT(EPOCH ...) здесь не нужен.
               ((date_trunc('week', a.occurred_at AT TIME ZONE :tz)::date
                 - c.cohort_week) / 7)::int AS week_offset,
               count(DISTINCT a.subscriber_id) AS retained
          FROM cohorts c
          JOIN subscriber_activity a ON a.subscriber_id = c.id
         GROUP BY 1, 2, 3
    )
    SELECT gen_random_uuid(), s.cohort_week, a.week_offset, s.kind,
           s.cohort_size, a.retained,
           round((a.retained::numeric / nullif(s.cohort_size, 0))::numeric, 4), now()
      FROM sized s JOIN active a USING (cohort_week, kind)
     WHERE a.week_offset >= 0
    ON CONFLICT (cohort_week, week_offset, kind) DO UPDATE SET
        cohort_size = EXCLUDED.cohort_size, retained = EXCLUDED.retained,
        retention_rate = EXCLUDED.retention_rate, updated_at = now()
    """
)

ENGAGEMENT_ROLLUP = text(
    """
    INSERT INTO metrics_engagement_daily (
        id, day, digests_sent, items_delivered, feedback_total,
        feedback_very_interesting, feedback_useful, feedback_not_interesting,
        feedback_saved, feedback_deeper, unique_reactors, empty_digests,
        engagement_rate, avg_items_per_digest, updated_at)
    SELECT gen_random_uuid(),
           g.day,
           coalesce(d.sent, 0),
           coalesce(d.items, 0),
           coalesce(f.total, 0),
           coalesce(f.very_interesting, 0),
           coalesce(f.useful, 0),
           coalesce(f.not_interesting, 0),
           coalesce(f.saved, 0),
           coalesce(f.deeper, 0),
           coalesce(f.reactors, 0),
           coalesce(d.empty, 0),
           -- Доля доставленных материалов, на которые вообще отреагировали.
           -- Ноль доставленных даёт NULL, а не деление на ноль.
           round((coalesce(f.total, 0)::numeric
                  / nullif(d.items, 0))::numeric, 4),
           round((coalesce(d.items, 0)::numeric
                  / nullif(d.sent, 0))::numeric, 2),
           now()
      FROM (SELECT gs::date AS day
              FROM generate_series(
                     CAST(:day_from AS date), CAST(:day_to AS date), '1 day'
                   ) AS gs) g
      LEFT JOIN (
            SELECT (dg.sent_at AT TIME ZONE :tz)::date AS day,
                   count(*) FILTER (WHERE dg.status = 'sent') AS sent,
                   count(*) FILTER (WHERE dg.status = 'skipped') AS empty,
                   coalesce(sum(items.n), 0) AS items
              FROM digests dg
              LEFT JOIN LATERAL (
                    SELECT count(*) AS n FROM digest_items di WHERE di.digest_id = dg.id
              ) items ON true
             WHERE dg.sent_at IS NOT NULL
             GROUP BY 1
           ) d ON d.day = g.day
      LEFT JOIN (
            SELECT (created_at AT TIME ZONE :tz)::date AS day,
                   count(*) AS total,
                   count(*) FILTER (WHERE kind = 'very_interesting') AS very_interesting,
                   count(*) FILTER (WHERE kind = 'useful') AS useful,
                   count(*) FILTER (WHERE kind = 'not_interesting') AS not_interesting,
                   count(*) FILTER (WHERE kind = 'save') AS saved,
                   count(*) FILTER (WHERE kind = 'deeper') AS deeper,
                   count(DISTINCT subscriber_id) AS reactors
              FROM feedback
             GROUP BY 1
           ) f ON f.day = g.day
     WHERE g.day BETWEEN :day_from AND :day_to
    ON CONFLICT (day) DO UPDATE SET
        digests_sent = EXCLUDED.digests_sent,
        items_delivered = EXCLUDED.items_delivered,
        feedback_total = EXCLUDED.feedback_total,
        feedback_very_interesting = EXCLUDED.feedback_very_interesting,
        feedback_useful = EXCLUDED.feedback_useful,
        feedback_not_interesting = EXCLUDED.feedback_not_interesting,
        feedback_saved = EXCLUDED.feedback_saved,
        feedback_deeper = EXCLUDED.feedback_deeper,
        unique_reactors = EXCLUDED.unique_reactors,
        empty_digests = EXCLUDED.empty_digests,
        engagement_rate = EXCLUDED.engagement_rate,
        avg_items_per_digest = EXCLUDED.avg_items_per_digest,
        updated_at = now()
    """
)

SCOPES = {
    "harvest": HARVEST_ROLLUP,
    "subscribers": SUBSCRIBERS_ROLLUP,
    "delivery": DELIVERY_ROLLUP,
    "engagement": ENGAGEMENT_ROLLUP,
}
