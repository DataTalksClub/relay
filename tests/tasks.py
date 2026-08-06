from django.tasks import task

import taskdeck


@task()
def simple_ok(value=1):
    return value * 2


@task()
def simple_fail():
    raise RuntimeError("intentional failure")


@task()
def child(n):
    return n


@task()
def fanout(count=3):
    """Fan-out parent: declares its denominator, then enqueues children.

    Nothing is required of the children -- they become children purely by
    being enqueued while this task is the ambient running task.
    """
    taskdeck.set_total(count, message=f"enqueuing {count}")
    for i in range(count):
        child.enqueue(i)
    return count


@task()
def loop_with_progress(n=4):
    taskdeck.set_total(n)
    for i in range(n):
        taskdeck.report(i + 1, message=f"item {i + 1}")
    return n


@task()
def tagged():
    taskdeck.set_entity("campaign", "42")
    taskdeck.set_owner("client-a")
    return "ok"
