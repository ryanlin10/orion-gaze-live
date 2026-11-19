import io
import time

from PIL import Image

from gazelive.capture import PICTURE_SIZE, FakeScreen, ScreenFeed


class Sink:
    def __init__(self):
        self.pictures = []

    def picture(self, jpeg):
        self.pictures.append(jpeg)


def test_screen_feed_sends_jpeg_when_the_screen_changes():
    sink = Sink()
    ScreenFeed(lambda: sink, grabber=FakeScreen(), hz=20).start()
    time.sleep(2.3)                     # the fake page changes once a second
    assert 2 <= len(sink.pictures) <= 4
    picture = Image.open(io.BytesIO(sink.pictures[0]))
    assert picture.format == "JPEG" and picture.size == PICTURE_SIZE


def test_screen_feed_idles_without_a_stream():
    feed = ScreenFeed(lambda: None, grabber=FakeScreen(), hz=20).start()
    time.sleep(0.3)
    assert feed.status == "on"
