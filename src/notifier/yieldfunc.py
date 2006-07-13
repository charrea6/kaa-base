# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# yieldfunc.py - Yield decorator and helper functions
# -----------------------------------------------------------------------------
# $Id: callback.py 1526 2006-04-29 14:55:39Z tack $
#
# This file contains a decorator usefull for functions that may need more
# time to execute and that needs more than one step to fullfill the task.
#
# A caller of a function decorated with 'yield_execution' will either get the
# return value of the function call or an InProgress object in return. The
# first is similar to a normal function call, if an InProgress object is
# returned, this means that the function is still running. The object has a
# 'connect' function to connect a callback to get the results of the function
# call when it is done.
#
# A function decorated with 'yield_execution' can't use 'return' to return the
# result of the function call. Instead it has to use yield to do this. Besides
# a normal return, the function can also return 'YieldContinue' in the yield
# statement. In that case, the function call continues at this point in the
# next notifier iteration. If the function itself has to wait for a result of
# a function call (either another yield function are something else working
# async), it can create a 'YieldCallback' object, add this as callback to the
# function it is calling and return this object using yield. In this case, the
# function will continue at this point when the other async call is finished.
# The function can use the 'get' function of the 'YieldCallback' to get the
# result of the async call.
#
# The 'yield_execution' decorator has a parameter intervall. This is the
# intervall used to schedule when the function should continue after a yield.
# The default value is 0, the first iteration is always called without a timer.
#
# -----------------------------------------------------------------------------
# kaa-notifier - Notifier Wrapper
# Copyright (C) 2006 Dirk Meyer, et al.
#
# First Version: Dirk Meyer <dmeyer@tzi.de>
# Maintainer:    Dirk Meyer <dmeyer@tzi.de>
#
# Please see the file AUTHORS for a complete list of authors.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MER-
# CHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#
# -----------------------------------------------------------------------------

__all__ = [ 'InProgress', 'YieldContinue', 'YieldCallback', 'yield_execution' ]

from callback import Callback
from timer import Timer

YieldContinue = object()

class InProgress(Callback):

    def __init__(self):
        self._callback = None

    def connect(self, callback, *args, **kwargs):
        Callback.__init__(self, callback, *args, **kwargs)

    def __call__(self, *args, **kwargs):
        if self._callback:
            super(InProgress, self).__call__(*args, **kwargs)


class YieldCallback(object):

    def __call__(self, result):
        self.result = result
        self._timer.start(self._interval)

    def connect(self, timer, interval):
        self._timer = timer
        self._interval = interval
        
    def __cmp__(self, obj):
        return not obj == self.__class__

    def get(self):
        return self.result

    
def yield_execution(interval=0):
    """
    """
    def decorator(func):

        class YieldFunction(InProgress):

            def __init__(self, function, status):
                InProgress.__init__(self)
                self._yield__function = function
                self._timer = Timer(self.step)
                if status == YieldContinue:
                    self._timer.start(interval)
                else:
                    status.connect(self._timer, interval)
                
            def step(self):
                result = self._yield__function()
                if result == YieldContinue:
                    return True
                self._timer.stop()
                if isinstance(result, YieldCallback):
                    result.connect(self._timer, interval)
                    return False
                self._timer = None
                self(result)
                return False

        def newfunc(*args, **kwargs):
            function = func(*args, **kwargs).next
            result = function()
            if not result in (YieldContinue, YieldCallback):
                # everything went fine, return result
                return result
            # we need a step callback to finish this later
            return YieldFunction(function, result)

        newfunc.func_name = func.func_name
        return newfunc

    return decorator