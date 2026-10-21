# Blocking-call audit: before and after

## Steps for one request

| Step | State | Working on / waiting for | Approx. duration (estimates) |
| --- | --- | --- | --- |
| Receive request | Waiting | Socket data | Milliseconds |
| Parse and validate input | Computing | JSON and 64 pixel values | Milliseconds or less |
| `time.sleep(1)` | Waiting | One-second timer | About 1 second |
| `model.predict(...)` | Computing | Loaded digits classifier | Milliseconds |
| Serialize response | Computing | Prediction JSON | Milliseconds or less |
| Send response | Computing / Waiting | Socket output / writability if needed | Milliseconds |

## Measurements: 10 concurrent requests, one server worker

| Measurement | Before (seconds) | After (seconds) |
| --- | ---: | ---: |
| Request 1 | 1.034650 | 1.059389 |
| Request 2 | 2.025374 | 1.049023 |
| Request 3 | 3.030707 | 1.050618 |
| Request 4 | 4.037387 | 1.049606 |
| Request 5 | 5.043012 | 1.047121 |
| Request 6 | 6.048244 | 1.048852 |
| Request 7 | 7.048753 | 1.050597 |
| Request 8 | 8.056270 | 1.050258 |
| Request 9 | 9.062929 | 1.046270 |
| Request 10 | 10.070233 | 1.047541 |
| **Batch wall-clock time** | **10.086974** | **1.060639** |
| **Slowest request** | **10.070233 (#10)** | **1.059389 (#1)** |

Both versions use `time.sleep(1)` to simulate slow inference because the model
alone is too fast to show the effect clearly. Moving the sleep and synchronous
model call into `await asyncio.to_thread(run_prediction)` lets requests overlap
in worker threads instead of blocking the event loop, reducing queueing and
batch time without making the model itself compute faster.
