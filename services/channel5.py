"""
Credit to Diazole(https://github.com/Diazole/my5-dl) for solving the keys 

Author: stabbedbybrick

Info:
Channel5 now offers up to 1080p

"""

import base64
import subprocess
import json
import hmac
import hashlib
import shutil

from urllib.parse import urlparse, urlunparse
from collections import Counter
from datetime import datetime

import click

from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from helpers.utilities import (
    info,
    string_cleaning,
    set_save_path,
    print_info,
    set_filename,
)
from helpers.cdm import local_cdm, remote_cdm
from helpers.titles import Episode, Series, Movie, Movies
from helpers.args import Options, get_args
from helpers.config import Config


class CHANNEL5(Config):
    def __init__(self, config, srvc, **kwargs):
        super().__init__(config, srvc, **kwargs)

        self.get_options()

    def get_data(self, url: str) -> dict:
        show = urlparse(url).path.split("/")[2]
        url = self.srvc["my5"]["api"]["content"].format(show=show)

        return self.client.get(url).json()

    def get_series(self, url: str) -> Series:
        data = self.get_data(url)

        return Series(
            [
                Episode(
                    id_=None,
                    service="MY5",
                    title=episode.get("sh_title"),
                    season=int(episode["sea_num"]),
                    number=int(episode["ep_num"]),
                    name=episode.get("title"),
                    year=None,
                    data=episode.get("id"),
                    description=episode.get("s_desc"),
                )
                for episode in data["episodes"]
            ]
        )

    def get_movies(self, url: str) -> Movies:
        data = self.get_data(url)

        return Movies(
            [
                Movie(
                    id_=None,
                    service="MY5",
                    title=movie["sh_title"],
                    year=None,
                    name=movie["sh_title"],
                    data=movie.get("id"),
                    synopsis=movie.get("s_desc"),
                )
                for movie in data["episodes"]
            ]
        )

    def decrypt_data(self, media: str) -> tuple:
        key = base64.b64decode(self.srvc["my5"]["keys"]["aes"])

        r = self.client.get(media)
        if not r.is_success:
            print(f"{r}\n{r.content}")
            shutil.rmtree(self.tmp)
            exit(1)

        content = r.json()

        iv = base64.urlsafe_b64decode(content["iv"])
        data = base64.urlsafe_b64decode(content["data"])

        cipher = AES.new(key=key, iv=iv, mode=AES.MODE_CBC)
        decrypted_data = unpad(cipher.decrypt(data), AES.block_size)
        return json.loads(decrypted_data)

    def get_playlist(self, asset_id: str) -> tuple:
        secret = self.srvc["my5"]["keys"]["hmac"]

        timestamp = datetime.now().timestamp()
        vod = self.srvc["my5"]["api"]["vod"].format(
            id=asset_id, timestamp=f"{timestamp}"
        )
        sig = hmac.new(base64.b64decode(secret), vod.encode(), hashlib.sha256)
        auth = base64.urlsafe_b64encode(sig.digest()).decode()
        vod += f"&auth={auth}"

        data = self.decrypt_data(vod)
        mpd_url = [x["renditions"][0]["url"] for x in data["assets"] if x["drm"] == "widevine"][0]
        lic_url = [x["keyserver"] for x in data["assets"] if x["drm"] == "widevine"][0]

        parse = urlparse(mpd_url)
        _path = parse.path.split("/")
        _path[-1] = f"{data['id']}A.mpd" if "A-tt" in _path[-1] else f"{data['id']}.mpd"
        manifest = urlunparse(parse._replace(path="/".join(_path)))

        return manifest, lic_url

    def get_pssh(self, soup: str) -> str:
        kid = (
            soup.select_one("ContentProtection")
            .attrs.get("cenc:default_KID")
            .replace("-", "")
        )
        array_of_bytes = bytearray(b"\x00\x00\x002pssh\x00\x00\x00\x00")
        array_of_bytes.extend(bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed"))
        array_of_bytes.extend(b"\x00\x00\x00\x12\x12\x10")
        array_of_bytes.extend(bytes.fromhex(kid.replace("-", "")))
        return base64.b64encode(bytes.fromhex(array_of_bytes.hex())).decode("utf-8")

    def get_mediainfo(self, manifest: str, quality: str) -> str:
        self.soup = BeautifulSoup(self.client.get(manifest), "xml")
        pssh = self.get_pssh(self.soup)
        elements = self.soup.find_all("Representation")
        heights = sorted(
            [int(x.attrs["height"]) for x in elements if x.attrs.get("height")],
            reverse=True,
        )

        if quality is not None:
            if int(quality) in heights:
                return quality, pssh
            else:
                closest_match = min(heights, key=lambda x: abs(int(x) - int(quality)))
                info(f"Resolution not available. Getting closest match:")
                return closest_match, pssh

        return heights[0], pssh

    def get_content(self, url: str) -> object:
        if self.movie:
            with self.console.status("Fetching titles..."):
                content = self.get_movies(self.url)
                title = string_cleaning(str(content))

            info(f"{str(content)}\n")

        else:
            with self.console.status("Fetching titles..."):
                content = self.get_series(url)
                for episode in content:
                    episode.name = episode.get_filename()

                title = string_cleaning(str(content))
                seasons = Counter(x.season for x in content)
                num_seasons = len(seasons)
                num_episodes = sum(seasons.values())

            info(
                f"{str(content)}: {num_seasons} Season(s), {num_episodes} Episode(s)\n"
            )

        return content, title

    def get_options(self) -> None:
        opt = Options(self)
        content, title = self.get_content(self.url)

        if self.episode:
            downloads = opt.get_episode(content)
        if self.season:
            downloads = opt.get_season(content)
        if self.complete:
            downloads = opt.get_complete(content)
        if self.movie:
            downloads = opt.get_movie(content)
        if self.titles:
            opt.list_titles(content)

        for download in downloads:
            self.download(download, title)

    def download(self, stream: object, title: str) -> None:
        with self.console.status("Getting media info..."):
            manifest, lic_url = self.get_playlist(stream.data)
            res, pssh = self.get_mediainfo(manifest, self.quality)

        with self.console.status("Getting decryption keys..."):
            keys = (
                remote_cdm(pssh, lic_url, self.client)
                if self.remote
                else local_cdm(pssh, lic_url, self.client)
            )
            with open(self.tmp / "keys.txt", "w") as file:
                file.write("\n".join(keys))

        self.filename = set_filename(self, stream, res, audio="AAC2.0")
        self.save_path = set_save_path(stream, self.config, title)
        self.manifest = manifest
        self.key_file = self.tmp / "keys.txt"
        self.sub_path = None

        if self.info:
            print_info(self, stream, keys)

        info(f"{stream.name}")
        for key in keys:
            info(f"{key}")
        click.echo("")

        args, file_path = get_args(self, res)

        if not file_path.exists():
            try:
                subprocess.run(args, check=True)
            except:
                raise ValueError("Download failed or was interrupted")
        else:
            info(f"{self.filename} already exist. Skipping download\n")
            self.sub_path.unlink() if self.sub_path else None
            pass