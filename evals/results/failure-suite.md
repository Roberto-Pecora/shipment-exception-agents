# Failure drills

| Drill | Result | Detail |
|---|:-:|---|
| poison message isolated to DLQ | pass | dlq=1, healthy cases notified=1 |
| agent worker crash: no double rebooking | pass | rebooks=1, customer notifications=1 |
| transient sink failure retried | pass | notified=1 after 1 transient failure, dlq=0 |
| persistent sink failure: DLQ, partition keeps flowing | pass | dlq=1 (x-attempts=2), later case notified |
| redelivery storm absorbed | pass | 100 deliveries of 20 events -> 20 notifications |
