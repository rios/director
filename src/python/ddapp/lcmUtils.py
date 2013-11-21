import lcm
import PythonQt
from PythonQt import QtCore


class GlobalLCM(object):

  _handle = None
  _lcmThread = None

  @classmethod
  def get(cls):
      if cls._handle == None:
          cls._handle = lcm.LCM()
      return cls._handle

  @classmethod
  def getThread(cls):
      if cls._lcmThread == None:
          cls._lcmThread = PythonQt.dd.ddLCMThread(PythonQt.QtCore.QCoreApplication.instance())
          cls._lcmThread.start()
      return cls._lcmThread


def getGlobalLCM():
    return GlobalLCM.get()


def getGlobalLCMThread():
    return GlobalLCM.getThread()


def captureMessage(channel, messageClass, lcmHandle=None):

    lcmHandle = lcmHandle or getGlobalLCM()

    messages = []
    def handleMessage(channel, messageData):
        messages.append(messageClass.decode(messageData))

    subscription = lcmHandle.subscribe(channel, handleMessage)

    while not messages:
        lcmHandle.handle()

    lcmHandle.unsubscribe(subscription)
    return messages[0]


def captureMessageAsync(channel, messageClass):

    lcmThread = getGlobalLCMThread()
    sub = PythonQt.dd.ddLCMSubscriber(channel)

    messages = []
    def handleMessage(messageData):
        messages.append(messageClass.decode(messageData.data()))
        lcmThread.removeSubscriber(sub)

    sub.connect('messageReceived(const QByteArray&)', handleMessage)
    lcmThread.addSubscriber(sub)

    while not messages:
        yield None

    yield messages[0]


def captureMessageCallback(channel, messageClass, callback):

    lcmThread = getGlobalLCMThread()
    sub = PythonQt.dd.ddLCMSubscriber(channel)
    messages = []
    def handleMessage(messageData):
        lcmThread.removeSubscriber(sub)
        if not messages:
            messages.append(messageClass.decode(messageData.data()))
            callback(messages[-1])

    sub.connect('messageReceived(const QByteArray&)', handleMessage)
    lcmThread.addSubscriber(sub)


def publish(channel, message):
    getGlobalLCM().publish(channel, message.encode())
