import io
import struct
import unittest

import srt

from backend.embedded_subtitles import extract_indexed, SparseReader
import httpx


def vint(value):
    for width in range(1, 9):
        if value < (1 << (7 * width)) - 1:
            return ((1 << (7 * width)) | value).to_bytes(width, "big")
    raise ValueError(value)


def element(ident, data):
    return ident.to_bytes((ident.bit_length()+7)//8, "big") + vint(len(data)) + data


def uint(ident, value):
    return element(ident, value.to_bytes(max(1,(value.bit_length()+7)//8), "big"))


def fixture(*, forced=0, codec="S_TEXT/ASS", index_count=2, stats_count=2, invalid_duration=False, language="eng", track_offset=0, texts=None):
    payloads = [f"{i},0,Default,,0,0,0,,Line {i+1}".encode() if codec == "S_TEXT/ASS"
                else f"Line {i+1}".encode() for i in range(2)]
    if texts is not None:
        payloads=[text.encode() for text in texts]
    track = element(0xAE, uint(0xD7,1)+uint(0x73C5,77)+uint(0x83,17)+uint(0x55AA,forced)
                    +element(0x86,codec.encode())+element(0x22B59C,language.encode())
                    +element(0x536E,b"Full dialogue")+uint(0x537F,track_offset))
    tracks = element(0x1654AE6B,track)
    info = element(0x1549A966,uint(0x2AD7B1,1000000))
    cluster_time=uint(0xE7,1000)
    blocks=[]
    relative=[]
    for i,payload in enumerate(payloads):
        relative.append(len(cluster_time)+sum(map(len,blocks)))
        block=element(0xA1,b'\x81'+struct.pack('>hB',i*3000,0)+payload)
        blocks.append(element(0xA0,block+uint(0x9B,2000)))
    cluster=element(0x1F43B675,cluster_time+b''.join(blocks))
    stat = lambda key,value: element(0x67C8,element(0x45A3,key.encode())+element(0x4487,str(value).encode()))
    tags=element(0x1254C367,element(0x7373,element(0x63C0,uint(0x63C5,77))
                 +stat("NUMBER_OF_FRAMES",stats_count)+stat("NUMBER_OF_BYTES",sum(map(len,payloads)))))
    # Large unrelated data forces actual random access rather than a scan.
    padding=element(0xEC,b'x'*(1024*1024))
    seek=b''
    for _ in range(5):
        positions={0x1654AE6B:len(seek),0x1549A966:len(seek)+len(tracks)}
        cluster_pos=len(seek)+len(tracks)+len(info)+len(padding)
        cues=element(0x1C53BB6B,b''.join(element(0xBB,uint(0xB3,1000+i*3000)
            +element(0xB7,uint(0xF7,1)+uint(0xF1,cluster_pos)+uint(0xF0,relative[i])
                     +uint(0xB2,1000 if invalid_duration else 2000)))for i in range(index_count)))
        positions[0x1C53BB6B]=cluster_pos+len(cluster)
        positions[0x1254C367]=positions[0x1C53BB6B]+len(cues)
        seek=element(0x114D9B74,b''.join(element(0x4DBB,element(0x53AB,k.to_bytes(4,'big'))+uint(0x53AC,v))
                                      for k,v in positions.items()))
    return element(0x1A45DFA3,b'')+element(0x18538067,seek+tracks+info+padding+cluster+cues+tags)


class EmbeddedSubtitleTests(unittest.TestCase):
    def test_indexed_ass_and_srt_preserve_container_timestamps(self):
        for codec in ("S_TEXT/ASS", "S_TEXT/UTF8"):
            with self.subTest(codec=codec):
                result=extract_indexed(io.BytesIO(fixture(codec=codec)),("en",))
                self.assertIsNotNone(result)
                cues=list(srt.parse(result.data.decode()))
                self.assertEqual([c.content for c in cues],["Line 1","Line 2"])
                self.assertEqual([c.start.total_seconds()for c in cues],[1,4])
                self.assertEqual([c.end.total_seconds()for c in cues],[3,6])

    def test_sparse_index_is_not_mistaken_for_complete_subtitles(self):
        self.assertIsNone(extract_indexed(io.BytesIO(fixture(index_count=1)),("en",)))
        self.assertIsNone(extract_indexed(io.BytesIO(fixture(stats_count=3)),("en",)))

    def test_plain_srt_text_is_not_interpreted_as_ass_markup(self):
        texts=["<i>Hello</i> {literal braces}","Second line"]
        result=extract_indexed(io.BytesIO(fixture(codec="S_TEXT/UTF8",texts=texts)),("en",))
        self.assertEqual([cue.content for cue in srt.parse(result.data.decode())],texts)

    def test_forced_unsupported_and_wrong_language_tracks_are_not_selected(self):
        for options in ({'forced':1},{'codec':'S_HDMV/PGS'},{'language':'jpn'}):
            with self.subTest(options=options):
                self.assertIsNone(extract_indexed(io.BytesIO(fixture(**options)),("en",)))

    def test_index_duration_must_match_the_actual_packet(self):
        self.assertIsNone(extract_indexed(io.BytesIO(fixture(invalid_duration=True)),("en",)))

    def test_unhandled_track_timing_adjustments_are_not_ignored(self):
        self.assertIsNone(extract_indexed(io.BytesIO(fixture(track_offset=100)),("en",)))

    def test_sparse_http_reads_skip_unrelated_media_bytes(self):
        data=fixture()
        requests=[]
        def read(headers):
            start,end=map(int,headers['Range'][6:].split('-'))
            requests.append((start,end))
            return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}'},content=data[start:end+1])
        reader=SparseReader(len(data),read)
        result=extract_indexed(reader,('en',))
        self.assertIsNotNone(result)
        self.assertLess(reader.cache.fetched,100000)
        self.assertTrue(requests)
        self.assertTrue(all(end-start+1 <= 16384 for start,end in requests))
        self.assertFalse(any(65536 < start < 1000000 for start,_ in requests))
