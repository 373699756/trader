"""Explicit diagnostic fields for job timings; no internal object serialization."""

from trader.training.application.resources import TrainingExecutionTiming, TrainingResourceTiming


def _resource(timing: TrainingResourceTiming) -> dict[str, object]:
    return {
        "wait_seconds": timing.wait_seconds,
        "hold_seconds": timing.hold_seconds,
        "acquired_count": timing.acquired_count,
        "failed_acquisitions": timing.failed_acquisitions,
        "inflight_count": timing.inflight_count,
    }


def ProjectTrainingExecutionTiming(timing: TrainingExecutionTiming) -> dict[str, object]:
    return {
        "source_read_seconds": timing.source_read_seconds,
        "source_read_thread_cpu_seconds": timing.source_read_thread_cpu_seconds,
        "thread_cpu_scope": "calling_thread_only; excludes_other_threads_and_native_workers; task_values_final_on_exit",
        "peak_cpu_reservations": timing.peak_cpu_reservations,
        "peak_memory_reservations": timing.peak_memory_reservations,
        "profiles": [
            {
                "profile": item.profile,
                "started_after_seconds": item.started_after_seconds,
                "finished_after_seconds": item.finished_after_seconds,
                "native_id": item.native_id,
                "thread_cpu_seconds": item.thread_cpu_seconds,
                "queue_send_seconds": item.queue_send_seconds,
                "queue_receive_seconds": item.queue_receive_seconds,
                "cpu": _resource(item.cpu),
                "memory": _resource(item.memory),
                "cpu_held_while_memory_wait_seconds": item.cpu_held_while_memory_wait_seconds,
            }
            for item in timing.profiles
        ],
    }


__all__ = ["ProjectTrainingExecutionTiming"]
