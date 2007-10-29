import sys
import kaa

# install special kaa reactor
import kaa.notifier.reactor
kaa.notifier.reactor.install()

# get reactor
from twisted.internet import reactor

def twisted_callback1():
    print "twisted", kaa.notifier.is_mainthread()
    
def twisted_callback2():
    print "twisted (shutdown)", kaa.notifier.is_mainthread()
    reactor.stop()
    
def kaa_callback():
    print 'kaa', kaa.notifier.is_mainthread()
    # sys.exit(0)
    
reactor.callLater(2.5, twisted_callback1)
reactor.callLater(3.5, twisted_callback2)
kaa.notifier.Timer(kaa_callback).start(1)

# you can either call notifier.main() or reactor.run()
reactor.run()
# kaa.main()

print 'stop'