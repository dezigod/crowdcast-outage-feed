import base64
from copy import deepcopy
import json
import unittest
from unittest.mock import MagicMock, patch

from outage_backend.github_delivery import FEED, HEALTH, MAX_PUBLIC_BYTES, GitHubDataBranch, public_feed
from outage_backend.tests.test_pipeline import build


def encoded(body):
    return {"encoding":"base64", "size":len(body), "content":base64.b64encode(body).decode()}


class LargeFeedDeliveryTests(unittest.TestCase):
    def test_contents_reads_request_documented_large_file_object_media_type(self):
        remote=GitHubDataBranch("example/test","synthetic")
        response=MagicMock()
        response.__enter__.return_value.read.return_value=b'{}'
        with patch("outage_backend.github_delivery.urllib.request.OpenerDirector.open",return_value=response) as send:
            remote.request("/contents/outages.json?ref=tip")
        self.assertEqual(send.call_args.args[0].get_header("Accept"), "application/vnd.github.object+json")

    def test_detailed_feed_over_one_megabyte_round_trips_through_exact_blob(self):
        feed=build()
        feed["sourceSummary"]["locations"]=["SYNTHETIC LOCATION " + "A"*200] * 6000
        body=(json.dumps(feed,sort_keys=True,indent=2)+"\n").encode()
        self.assertGreater(len(body),1_048_576)
        self.assertLess(len(body),MAX_PUBLIC_BYTES)
        public_feed(feed)
        health={"status":"ok","error":None,"nextAttemptAt":"2026-10-15T16:05:00Z"}
        hbody=json.dumps(health).encode()
        remote=GitHubDataBranch("example/test","synthetic")
        requests=[]
        def request(path,**kwargs):
            requests.append(path)
            if path=="/git/ref/heads/data":return {"object":{"sha":"tip"}}
            if path=="/git/commits/tip":return {"tree":{"sha":"tree"}}
            if path=="/contents/"+HEALTH+"?ref=tip":return encoded(hbody)
            if path=="/contents/"+FEED+"?ref=tip":
                return {"size":len(body),"encoding":"none","content":"","sha":"a"*40,
                        "download_url":"https://not-used.example/"}
            if path=="/git/blobs/"+"a"*40:return encoded(body)
            self.fail(path)
        remote.request=request
        restored=remote.load()
        self.assertEqual(restored[FEED],feed)
        public_feed(restored[FEED])
        self.assertEqual(restored[HEALTH],health)
        self.assertEqual(requests[-1],"/git/blobs/"+"a"*40)
        writes=[]
        remote.previous={}
        def publish_request(path,**kwargs):
            writes.append((path,kwargs))
            return {"sha":"new-tree" if path=="/git/trees" else "new-commit"}
        remote.request=publish_request
        remote.publish({FEED:feed})
        written=writes[0][1]["payload"]["tree"][0]["content"].encode()
        self.assertEqual(written,body)
        self.assertFalse(writes[-1][1]["payload"]["force"])

    def test_oversize_publication_stops_before_any_remote_write(self):
        feed=build();feed["sourceSummary"]["locations"]=["A"*1000]*2200
        remote=GitHubDataBranch("example/test","synthetic")
        remote.previous={};remote.tip="tip";remote.tree="tree"
        calls=[];remote.request=lambda *args,**kwargs:calls.append(args)
        with self.assertRaisesRegex(ValueError,"reload size limit"):
            remote.publish({FEED:feed})
        self.assertEqual(calls,[])
        self.assertEqual(remote.previous,{})

    def test_blob_fallback_rejects_bad_identity_oversize_and_mismatched_bytes(self):
        small=json.dumps(build()).encode()
        cases=[({"size":len(small),"encoding":"none","sha":"../private"},None),
               ({"size":MAX_PUBLIC_BYTES+1,"encoding":"none","sha":"a"*40},None),
               ({"size":len(small),"encoding":"none","sha":"a"*40},dict(encoded(small),size=1)),
               ({"size":len(small),"encoding":"none","sha":"a"*40},dict(encoded(small),content=""))]
        for metadata,blob in cases:
            with self.subTest(metadata=metadata,blob_size=blob and blob["size"]):
                remote=GitHubDataBranch("example/test","synthetic")
                def request(path,**kwargs):
                    if path=="/git/ref/heads/data":return {"object":{"sha":"tip"}}
                    if path=="/git/commits/tip":return {"tree":{"sha":"tree"}}
                    if path.startswith("/contents/"):return metadata
                    if path=="/git/blobs/"+"a"*40:return blob
                    self.fail(path)
                remote.request=request
                with self.assertRaises(ValueError):remote.load()


if __name__=="__main__":unittest.main()
