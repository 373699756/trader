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
        "sample_build_scope": "builder_wall_includes_input_wait_and_sqlite; thread_cpu_calling_thread_only",
        "queue_wait_scope": "blocking_retry_after_observed_full_or_empty; includes_call_overhead; subset_of_send_receive",
        "queue_peak_scope": "data_batches_under_queue_mutex; excludes_end_sentinel",
        "overlap_scope": "source_queue_sample_and_task_seconds_overlap; do_not_add",
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
                "first_batch_after_seconds": item.first_batch_after_seconds,
                "queue_full_wait_seconds": item.queue_full_wait_seconds,
                "queue_empty_wait_seconds": item.queue_empty_wait_seconds,
                "queue_full_wait_count": item.queue_full_wait_count,
                "queue_empty_wait_count": item.queue_empty_wait_count,
                "queue_peak_batches": item.queue_peak_batches,
                "sample_build": {
                    "count": item.sample_build.count,
                    "completed_count": item.sample_build.completed_count,
                    "inflight_count": item.sample_build.inflight_count,
                    "wall_seconds": item.sample_build.wall_seconds,
                    "thread_cpu_seconds": item.sample_build.thread_cpu_seconds,
                    "first_started_after_seconds": item.sample_build.first_started_after_seconds,
                    "last_finished_after_seconds": item.sample_build.last_finished_after_seconds,
                },
                "cpu": _resource(item.cpu),
                "memory": _resource(item.memory),
                "cpu_held_while_memory_wait_seconds": item.cpu_held_while_memory_wait_seconds,
            }
            for item in timing.profiles
        ],
    }


__all__ = ["ProjectTrainingExecutionTiming"]
