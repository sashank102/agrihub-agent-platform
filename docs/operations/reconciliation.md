# Orphaned runs and reconciliation

The API runs as one process. Task handles and live SSE queues are not in
PostgreSQL. Startup does not call the model or tools again.

## What startup does

1. Settle `pending` and `running` rows that this process is not executing.
2. Repair a terminal row that is missing its single `end` or `error` event.
3. Retry a terminal write that was queued in this process.

A completed checkpoint stays `completed`, including when run metadata was
missing. That does not turn graph success into `failed`. An interrupted
checkpoint becomes `interrupted`. Anything else becomes `interrupted` with
an explicit reason such as `process_restart` or `stale_active_run`.

If the terminal write cannot be saved, the reconciliation intent stays on the
active row (`error_details.reconciliation`) and in the process retry queue.
Admission of another `pending` or `running` row for that thread is rejected
with HTTP 409. The body includes `status`, `reconciliation_intent`, and
`graph_succeeded` so an operator can see the outcome without reading a
prompt. The next admission retries settlement. A thread is not left blocked
once PostgreSQL accepts the terminal write.

`uq_runs_one_active_per_thread` is a partial unique index. A race that passes
the in-process check still cannot insert a second active row.

## Cancel during task binding

Cancellation is stored on the row before the response returns. If the task
is not registered yet, the request waits for registration and then cancels
it. If registration does not complete, the reserved run is durably marked
`cancelled` before the response returns. A cancellation endpoint never claims
success while the durable status remains `pending` or `running`; an inability
to persist a terminal status returns a service error instead. Repeating the
cancel after the row is terminal returns that terminal status again.

## What not to do

Do not mark a completed checkpoint as `failed` because a metadata write was
late. Do not delete one of `checkpoints`, `checkpoint_blobs`, or
`checkpoint_writes` to "unstick" a thread. Do not start a second API worker
to drain the queue; it cannot see the first process's tasks and the advisory
lock refuses it.

External tools and model calls are not replayed. The checkpoint is the
resume point. The user sends a new run, or resumes an interrupt, after the
orphan row has a terminal status.

## Inspection

```sql
SELECT id, thread_id, status, cancellation_requested, error_details->'reconciliation' AS reconciliation
FROM platform.runs
WHERE status IN ('pending', 'running')
ORDER BY created_at;
```

`/ready` fails until the run manager and the process lock are up. After a
restart, reload the thread in the UI. History comes from the checkpoint and
the event log, not from the process that exited.
