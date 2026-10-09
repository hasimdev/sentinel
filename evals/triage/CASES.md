# Triage eval: practice incidents for review

Each incident is a scripted story. The AI sees only what its look-up tools return
(the incident, metrics, logs), never the story text or the right answer.

| # | Incident | Right answer | Correct cause (answer key) |
| --- | --- | --- | --- |
| 1 | **New release breaks checkout** (`bad-release-checkout`) | rollback | Release 0.2.0 (b7e41c2) introduced a checkout bug; 0.1.0 had no errors. |
| 2 | **Half the traffic on the new version, only it fails** (`canary-split`) | rollback | Release 0.2.0 (c41d8e7) fails checkouts; 0.1.0 serving the other half is healthy. |
| 3 | **New release breaks the product list** (`bad-release-products`) | rollback | Release 0.3.0 (d93a210) broke /products (KeyError on price field) right after deploy. |
| 4 | **New release makes checkout time out** (`bad-release-slow-db`) | rollback | Release 0.2.1 (e5f6071) added a slow query; checkouts time out since that deploy. |
| 5 | **New release, error rate just over the line** (`bad-release-borderline`) | rollback | Release 0.4.0 (f1029bd) added a validation bug rejecting valid checkouts. |
| 6 | **Someone turned on the 'break it' dial** (`fault-injection`) | investigate | Fault injection is switched on (fail_rate=0.5); not a code bug, rollback won't help. |
| 7 | **The payment provider is down** (`payment-provider-outage`) | investigate | External payment gateway outage; current release ran cleanly for 2 days. |
| 8 | **Errors on a long-running version, logs are down** (`logs-unavailable`) | investigate | Errors on a 3-day-old release with no recent deploy; logs unavailable, cause unknown. |
| 9 | **A sudden traffic surge overloads the shop** (`traffic-spike`) | investigate | Traffic surge (about 5x) exhausted the database connection pool; not a release issue. |
| 10 | **The failing instances run the OLD version** (`old-version-failing`) | investigate | Leftover 0.2.0 instances are failing; the current 0.3.0 is healthy, so rollback is wrong. |
| 11 | **Errors started before the latest release** (`errors-before-release`) | investigate | Errors began 20 minutes before 0.2.1 deployed and continue at the same rate. |
| 12 | **A feature flag switched on, no new code** (`feature-flag`) | investigate | Enabling the new_checkout_flow feature flag broke checkout; no code was deployed. |
| 13 | **A short blip that cleared by itself** (`blip-recovered`) | no action | A 3-minute blip that cleared by itself; no errors for the last 12 minutes. |
| 14 | **The bad release was already rolled back** (`already-rolled-back`) | no action | Bad release 0.2.0 was already rolled back to 0.1.0; errors stopped 8 minutes ago. |
| 15 | **A chaos test ended and the dial was turned off** (`chaos-test-ended`) | no action | Fault injection was turned off (restart with fail_rate=0); errors stopped 10 minutes ago. |
| 16 | **The payment provider came back** (`provider-recovered`) | no action | External payment gateway outage that recovered 9 minutes ago; checkouts healthy now. |

## The stories

### 1. New release breaks checkout -> **rollback**

Version 0.2.0 went out 25 minutes ago. Since then 45% of checkouts fail with a payment error. Version 0.1.0 ran cleanly for days before.

### 2. Half the traffic on the new version, only it fails -> **rollback**

0.1.0 and 0.2.0 are both running, each taking half the traffic. Only 0.2.0's checkouts fail (60%); 0.1.0 is fine.

### 3. New release breaks the product list -> **rollback**

Right after 0.3.0 deployed, the product list started failing for every visitor (500 errors). Checkout still works for people who already have a product page open.

### 4. New release makes checkout time out -> **rollback**

0.2.1 added a new database query. Since it deployed, checkouts take over 2 seconds and 30% time out (504). Before, checkout took about 5 ms.

### 5. New release, error rate just over the line -> **rollback**

After 0.4.0 deployed, 24% of checkouts fail with a new validation error that never appeared before. Just above the 20% alert line, but clearly new.

### 6. Someone turned on the 'break it' dial -> **investigate**

Half of checkouts fail with 'injected fault'. The startup log shows fail_rate=0.5 on both the current and the previous release, which failed just as much.

### 7. The payment provider is down -> **investigate**

Checkouts started failing 15 minutes ago with timeouts calling the outside payment gateway. The current release has run fine for 2 days; nothing was deployed today.

### 8. Errors on a long-running version, logs are down -> **investigate**

Checkout errors at 35% on a version that has run for 3 days. The log system is down, so the error messages can't be read. Metrics only.

### 9. A sudden traffic surge overloads the shop -> **investigate**

Traffic jumped to 5 times normal 12 minutes ago (a marketing email went out). Checkouts fail with 'connection pool exhausted'. No deploy for 2 days.

### 10. The failing instances run the OLD version -> **investigate**

0.3.0 is healthy. A few leftover instances still run 0.2.0, and those are the ones failing. Rolling back to 0.2.0 would make things worse.

### 11. Errors started before the latest release -> **investigate**

Checkout errors began 40 minutes ago on 0.2.0. 0.2.1 deployed 20 minutes ago and fails at the same rate. The release didn't cause it.

### 12. A feature flag switched on, no new code -> **investigate**

No deploy today. 18 minutes ago a log line shows the 'new_checkout_flow' feature flag was enabled; checkout errors started at the same moment.

### 13. A short blip that cleared by itself -> **no action**

Checkouts failed for about 3 minutes, then stopped on their own. No errors for the last 12 minutes and no deploy. Nothing left to do.

### 14. The bad release was already rolled back -> **no action**

0.2.0 broke checkout, then someone redeployed 0.1.0 eight minutes ago. Errors stopped right after. The problem is already handled.

### 15. A chaos test ended and the dial was turned off -> **no action**

Fault injection was on for a test. Ten minutes ago the shop restarted with fail_rate=0 and errors stopped.

### 16. The payment provider came back -> **no action**

The outside payment gateway timed out for 15 minutes, then recovered 9 minutes ago. Checkouts have succeeded since.

