# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# thread.py - Thread support for the Kaa Framework
# -----------------------------------------------------------------------------
# $Id$
#
# This module contains some wrapper classes for threading while running the
# main loop. It should only be used when non blocking handling is not
# possible. The main loop itself is not thread save, the the function called in
# the thread should not touch any variables inside the application which are
# not protected by by a lock.
#
# You can create a Thread object with the function and it's
# arguments. After that you can call the start function to start the
# thread. This function has an optional parameter with a callback
# which will be called from the main loop once the thread is
# finished. The result of the thread function is the parameter for the
# callback.
#
# In most cases this module is not needed, please add a good reason why you
# wrap a function in a thread.
#
# -----------------------------------------------------------------------------
# kaa.base - The Kaa Application Framework
# Copyright (C) 2005-2009 Dirk Meyer, Jason Tackaberry, et al.
#
# First Version: Dirk Meyer <dmeyer@tzi.de>
# Maintainer:    Dirk Meyer <dmeyer@tzi.de>
#                Jason Tackaberry <tack@urandom.ca>
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# -----------------------------------------------------------------------------

__all__ = [ 'MainThreadCallback', 'ThreadCallback', 'is_mainthread',
            'wakeup', 'set_as_mainthread', 'create_thread_notifier_pipe',
            'threaded', 'MAINTHREAD', 'synchronized' ]

# python imports
import sys
import os
import threading
import logging
import fcntl
import socket
import errno
import types

# kaa imports
import nf_wrapper as notifier
from callback import Callback
from async import InProgress
from utils import wraps, DecoratorDataStore, sysimport

# import python thread file
LockType = sysimport('thread').LockType

# get logging object
log = logging.getLogger('base')

# TODO: organize thread stuff into its own namespace

_thread_notifier_mainthread = threading.currentThread()
_thread_notifier_lock = threading.RLock()
_thread_notifier_queue = []

# For MainThread* callbacks. The pipe will be created when it is used the first
# time. This solves a nasty bug when you fork() into a second kaa based
# process without exec. If you have this pipe, communication will go wrong.
# (kaa.utils.daemonize does not have this problem.)
_thread_notifier_pipe = None

# internal list of named threads
_threads = {}

# For threaded decorator
MAINTHREAD = object()

def threaded(name=None, priority=0, async=True, progress=False):
    """
    The decorator makes sure the function is always called in the thread
    with the given name. The function will return an InProgress object if
    async=True (default), otherwise it will cause invoking the decorated
    function to block (the main loop is kept alive) and its result is
    returned. If progress is True, the first argument to the function is
    an InProgress.Progress object to return execution progress.

    If name=kaa.MAINTHREAD, the decorated function will be invoked from
    the main thread.  (In this case, currently the priority kwarg is
    ignored.)
    """
    if progress is True:
        progress = InProgress.Progress

    def decorator(func):
        @wraps(func, lshift=int(not not progress))
        def newfunc(*args, **kwargs):
            if progress:
                args = (progress(),) + args
            if name is MAINTHREAD:
                if not async and is_mainthread():
                    # Fast-path case: mainthread synchronous call from the mainthread
                    return func(*args, **kwargs)
                callback = MainThreadCallback(func)
            elif name:
                callback = NamedThreadCallback((name, priority), func)
            else:
                callback = ThreadCallback(func)
                callback.wait_on_exit(False)

            # callback will always return InProgress
            in_progress = callback(*args, **kwargs)
            if not async:
                return in_progress.wait()
            if progress:
                in_progress.progress = args[0]
            return in_progress

        newfunc.func_name = func.func_name
        return newfunc

    return decorator


class synchronized(object):
    """
    synchronized decorator and `with` statement similar to synchronized
    in Java. When decorating a non-member function, a lock or any class
    inheriting from object may be provided.

    :param obj: object were all calls should be synchronized to.
      if not provided it will be the object for member functions
      or an RLock for functions.
    """
    def __init__(self, obj=None):
        """
        Create a synchronized object. Note: when used on classes a new
        member _kaa_synchronized_lock will be added to that class.
        """
        if obj is None:
            # decorator in classes
            self._lock = None
            return
        if isinstance(obj, (threading._RLock, LockType)):
            # decorator from functions
            self._lock = obj
            return
        # with statement or function decorator with object
        if not hasattr(obj, '_kaa_synchronized_lock'):
            obj._kaa_synchronized_lock = threading.RLock()
        self._lock = obj._kaa_synchronized_lock

    def __enter__(self):
        """
        with statement enter
        """
        if self._lock is None:
            raise RuntimeError('synchronized in with needs a parameter')
        self._lock.acquire()
        return self._lock

    def __exit__(self, type, value, traceback):
        """
        with statement exit
        """
        self._lock.release()
        return False

    def __call__(self, func):
        """
        decorator init
        """
        def call(*args, **kwargs):
            """
            decorator call
            """
            lock = self._lock
            if lock is None:
                # Lock not specified, use one attached to decorated function.
                store = DecoratorDataStore(func, call, args)
                if 'synchronized_lock' not in store:
                    store.synchronized_lock = threading.RLock()
                lock = store.synchronized_lock

            lock.acquire()
            try:
                return func(*args, **kwargs)
            finally:
                lock.release()
        return call


def is_mainthread():
    """
    Return True if the current thread is the main thread.

    Note that the "main thread" is considered to be the thread in which the
    kaa main loop is running.  This is usually, but not necessarily, what
    Python considers to be the main thread.  (If you call kaa.main.run()
    in the main Python thread, then they are equivalent.)
    
    """
    # If threading module is None, assume main thread.  (Silences pointless
    # exceptions on shutdown.)
    return (not threading) or threading.currentThread() == _thread_notifier_mainthread


def wakeup():
    """
    Wake up main thread. A thread can use this function to wake up a mainloop
    waiting on a select.
    """
    if _thread_notifier_pipe and len(_thread_notifier_queue) == 0:
        os.write(_thread_notifier_pipe[1], "1")


def create_thread_notifier_pipe(new = True, purge = False):
    """
    Creates a new pipe for the thread notifier.  If new is True, a new pipe
    will always be created; if it is False, it will only be created if one
    already exists.  If purge is True, any previously queued work will be
    discarded.

    This is an internal function, but we export it for kaa.utils.daemonize.
    """
    global _thread_notifier_pipe
    log.info('create thread notifier pipe')

    if not _thread_notifier_pipe and not new:
        return
    elif _thread_notifier_pipe:
        # There is an existing pipe already, so stop monitoring it.
        notifier.socket_remove(_thread_notifier_pipe[0])

    if purge:
        _thread_notifier_lock.acquire()
        del _thread_notifier_queue[:]
        _thread_notifier_lock.release()

    _thread_notifier_pipe = os.pipe()
    fcntl.fcntl(_thread_notifier_pipe[0], fcntl.F_SETFL, os.O_NONBLOCK)
    fcntl.fcntl(_thread_notifier_pipe[1], fcntl.F_SETFL, os.O_NONBLOCK)
    notifier.socket_add(_thread_notifier_pipe[0], _thread_notifier_run_queue)

    if _thread_notifier_queue:
        # A thread is already running and wanted to run something in the
        # mainloop before the mainloop is started. In that case we need
        # to wakeup the loop ASAP to handle the requests.
        os.write(_thread_notifier_pipe[1], "1")


def set_as_mainthread():
    """
    Set the current thread as mainthread. This function SHOULD NOT be called
    from the outside, the loop function is setting the mainthread if needed.
    """
    global _thread_notifier_mainthread
    _thread_notifier_mainthread = threading.currentThread()
    if not _thread_notifier_pipe:
        # Make sure we have a pipe between the mainloop and threads. Since
        # loop() calls set_as_mainthread it is safe to assume the loop is
        # connected correctly. If someone calls step() without loop() and
        # without set_as_mainthread inter-thread communication does not work.
        create_thread_notifier_pipe()


def killall():
    """
    Kill all running job server. This function will be called by the main
    loop when it shuts down.
    """
    for j in _threads.values():
        j.stop()
        j.join()


def _thread_notifier_queue_callback(callback, args, kwargs, in_progress):
    _thread_notifier_lock.acquire()
    _thread_notifier_queue.append((callback, args, kwargs, in_progress))
    if len(_thread_notifier_queue) == 1:
        if _thread_notifier_pipe:
            os.write(_thread_notifier_pipe[1], "1")
    _thread_notifier_lock.release()


def _thread_notifier_run_queue(fd):
    try:
        os.read(_thread_notifier_pipe[0], 1000)
    except socket.error, (err, msg):
        if err == errno.EAGAIN:
            # Resource temporarily unavailable -- we are trying to read
            # data on a socket when none is avilable.  This should not
            # happen under normal circumstances, so log an error.
            log.error("Thread notifier pipe woke but no data available.")
    except OSError:
        pass
    _thread_notifier_lock.acquire()
    try:
        while _thread_notifier_queue:
            callback, args, kwargs, in_progress = _thread_notifier_queue.pop(0)
            try:
                in_progress.finish(callback(*args, **kwargs))
            except BaseException, e:
                # All exceptions, including SystemExit and KeyboardInterrupt,
                # are caught and thrown to the InProgress, because it may be
                # waiting in another thread.  However SE and KI are reraised
                # in here the main thread so they can be propagated back up
                # the mainloop.
                in_progress.throw(*sys.exc_info())
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
    finally:
        _thread_notifier_lock.release()
    return True


class MainThreadCallback(Callback):
    """
    Callback that is invoked from the main thread.
    """
    def __call__(self, *args, **kwargs):
        in_progress = InProgress()

        if is_mainthread():
            try:
                result = super(MainThreadCallback, self).__call__(*args, **kwargs)
            except BaseException, e:
                # All exceptions, including SystemExit and KeyboardInterrupt,
                # are caught and thrown to the InProgress, because it may be
                # waiting in another thread.  However SE and KI are reraised
                # in here the main thread so they can be propagated back up
                # the mainloop.
                in_progress.throw(*sys.exc_info())
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
            else:
                in_progress.finish(result)

            return in_progress

        _thread_notifier_queue_callback(self, args, kwargs, in_progress)

        # Return an InProgress object which the caller can connect to
        # or wait on.
        return in_progress


class ThreadInProgress(InProgress):
    def __init__(self, callback, *args, **kwargs):
        InProgress.__init__(self)
        self._callback = Callback(callback, *args, **kwargs)


    def __call__(self, *args, **kwargs):
        """
        Execute the callback.
        """
        if self._callback is None:
            return None
        try:
            result = self._callback()
        except:
            # FIXME: should we really be catching KeyboardInterrupt and SystemExit?
            MainThreadCallback(self.throw)(*sys.exc_info())
        else:
            if type(result) == types.GeneratorType or isinstance(result, InProgress):
                # Looks like the callback is yielding something, or callback is a
                # coroutine-decorated function.  Not supported (yet?).  In the
                # case of coroutines, the first entry will execute in the
                # thread, but subsequent entries (via the generator's next())
                # will be from the mainthread, which is almost certainly _not_
                # what is intended by threading a coroutine.
                log.warning('NYI: coroutines cannot (yet) be executed in threads.')
            MainThreadCallback(self.finish)(result)
        self._callback = None


    def active(self):
        """
        Return True if the callback is still waiting to be proccessed.
        """
        return self._callback is not None


    def stop(self):
        """
        Remove the callback from the thread schedule if still active.
        """
        self._callback = None


class ThreadCallback(Callback):
    """
    Notifier aware wrapper for threads. When a thread is started, it is
    impossible to fork the current process into a second one without exec both
    using the main loop because of the shared _thread_notifier_pipe.
    """
    _daemon = False

    def wait_on_exit(self, wait=False):
        """
        Wait for the thread on application exit. Default is True.
        """
        self._daemon = not wait


    def _create_thread(self, *args, **kwargs):
        """
        Create and start the thread.
        """
        cb = Callback._get_callback(self)
        async = ThreadInProgress(cb, *args, **kwargs)
        # create thread and setDaemon
        t = threading.Thread(target=async)
        t.setDaemon(self._daemon)
        # connect thread.join to the InProgress
        join = lambda *args, **kwargs: t.join()
        async.connect_both(join, join)
        # start the thread
        t.start()
        return async


    def _get_callback(self):
        """
        Return callable for this Callback.
        """
        return self._create_thread



class NamedThreadCallback(Callback):
    """
    A callback to run a function in a thread. This class is used by the
    threaded decorator, but it is also possible to use this call directly.
    """
    def __init__(self, thread_information, func, *args, **kwargs):
        Callback.__init__(self, func, *args, **kwargs)
        self.priority = 0
        if isinstance(thread_information, (list, tuple)):
            thread_information, self.priority = thread_information
        self._thread = thread_information


    def _create_job(self, *args, **kwargs):
        cb = Callback._get_callback(self)
        job = ThreadInProgress(cb, *args, **kwargs)
        job.priority = self.priority
        if not _threads.has_key(self._thread):
            _threads[self._thread] = _JobServer(self._thread)
        server = _threads[self._thread]
        server.add(job)
        return job


    def _get_callback(self):
        return self._create_job


class _JobServer(threading.Thread):
    """
    Thread processing NamedThreadCallback jobs.
    """
    def __init__(self, name):
        log.debug('start jobserver %s' % name)
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.condition = threading.Condition()
        self.stopped = False
        self.jobs = []
        self.name = name
        self.start()


    def stop(self):
        """
        Stop the thread.
        """
        self.condition.acquire()
        self.stopped = True
        self.condition.notify()
        self.condition.release()


    def add(self, job):
        """
        Add a NamedThreadCallback to the thread.
        """
        self.condition.acquire()
        self.jobs.append(job)
        self.jobs.sort(lambda x,y: -cmp(x.priority, y.priority))
        self.condition.notify()
        self.condition.release()


    def remove(self, job):
        """
        Remove a NamedThreadCallback from the schedule.
        """
        if job in self.jobs:
            self.condition.acquire()
            self.jobs.remove(job)
            self.condition.release()


    def run(self):
        """
        Thread main function.
        """
        while not self.stopped:
            # get a new job to process
            self.condition.acquire()
            while not self.jobs and not self.stopped:
                # nothing to do, wait
                self.condition.wait()
            if self.stopped:
                self.condition.release()
                continue
            job = self.jobs.pop(0)
            self.condition.release()
            job()
        # server stopped
        log.debug('stop thread %s' % self.name)