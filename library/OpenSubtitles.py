# Handles subtitle search and download through the OpenSubtitles API.
import json
import time
from pathlib import Path

import requests
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from library.subtitle_utils import SubtitleUtils, report_existing_subtitle


class OpenSubtitles:
    def __init__(
        self,
        username,
        password,
        api_key,
        user_agent,
        sync_audio_to_subs=False,
        hearing_impaired=False,
        auto_select=True,
        output_directory=None,
    ):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.user_agent = user_agent
        self.sync_audio_to_subs = sync_audio_to_subs
        self.hearing_impaired = hearing_impaired
        self.auto_select = auto_select
        self.output_directory = (
            Path(output_directory) if output_directory is not None else None
        )
        self.console = Console()
        self.subtitle_utils = SubtitleUtils()
        self.token = self.login()

    def _output_path(self, media_path, filename):
        directory = self.output_directory or Path(media_path).parent
        if self.output_directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        return directory / filename

    def _fresh_login(self):
        """POST a new token ignoring any cached value (used after a 401)."""
        url = "https://api.opensubtitles.com/api/v1/login"

        payload = {"username": self.username, "password": self.password}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Api-Key": self.api_key,
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            token = response.json()["token"]
            self.subtitle_utils.save_token(token)
            return token
        except requests.exceptions.RequestException as e:
            self.console.print(f"[bold red]Error during OpenSubtitles login: {e}[/]")
            return None
        except (KeyError, json.decoder.JSONDecodeError) as e:
            self.console.print(
                f"[bold red]Error parsing OpenSubtitles login response: {e}[/]"
            )
            return None
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error during login: {e}[/]")
            return None

    def login(self):
        token = self.subtitle_utils.read_token()
        if token:
            return token

        return self._fresh_login()

    def health(self):
        """Lightweight reachability probe for the OpenSubtitles API.

        Returns a dict so the TUI adapter can build a HealthResult without
        importing TUI types into the library layer.
        """
        if not self.api_key:
            return {
                "reachable": False,
                "authenticated": False,
                "latency_ms": None,
                "reason": "API key not configured",
            }

        url = "https://api.opensubtitles.com/api/v1/infos/formats"
        headers = {
            "Accept": "application/json",
            "Api-Key": self.api_key,
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        started = time.perf_counter()
        try:
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "reachable": True,
                "authenticated": bool(self.token),
                "latency_ms": latency_ms,
                "reason": f"reachable ({latency_ms} ms)",
            }
        except requests.exceptions.RequestException as exc:
            return {
                "reachable": False,
                "authenticated": False,
                "latency_ms": None,
                "reason": f" unreachable: {exc}",
            }

    def search(
        self,
        media_hash="",
        imdb_id="",
        media_name="",
        languages="en,ar",
    ):
        url = "https://api.opensubtitles.com/api/v1/subtitles"
        hearing_impaired = "include" if self.hearing_impaired else "exclude"
        params = {
            "languages": languages,
            "hearing_impaired": hearing_impaired,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Api-Key": self.api_key,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
        }
        if imdb_id:
            params["imdb_id"] = imdb_id

        if media_hash:
            params["moviehash"] = media_hash

        if media_name:
            params["query"] = media_name

        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            results = response.json()["data"]
            return results
        except requests.exceptions.RequestException as e:
            self.console.print(f"[bold red]Error during OpenSubtitles search: {e}[/]")
            return None
        except (KeyError, json.decoder.JSONDecodeError) as e:
            self.console.print(
                f"[bold red]Error parsing OpenSubtitles search response: {e}[/]"
            )
            return None
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error during search: {e}[/]")
            return None

    def _gather_candidates(self, path, language, query=""):
        media_path = Path(path)
        media_hash = ""
        if media_path.is_file():
            try:
                media_hash = self.subtitle_utils.hashFile(media_path) or ""
            except (OSError, ValueError):
                media_hash = ""
        effective_query = self.subtitle_utils.normalize_media_name(
            query.strip() or media_path.stem
        )
        queries = [effective_query]
        clean_titles = self.subtitle_utils._title_hypotheses(effective_query)
        if clean_titles:
            queries.append(clean_titles[0])
        queries.extend(self.subtitle_utils.get_alternate_names(effective_query) or [])
        queries = list(dict.fromkeys(queries))

        results = []
        request_failed = False
        if media_hash:
            found = self.search(
                media_hash=media_hash,
                media_name="",
                languages=language,
            )
            if found is None:
                request_failed = True
            else:
                results.extend(found)

        for media_name in dict.fromkeys(queries):
            found = self.search(
                media_hash="",
                media_name=media_name,
                languages=language,
            )
            if found is None:
                request_failed = True
                continue
            results.extend(found)

        unique_results = {}
        for row in results:
            unique_results.setdefault(row["id"], row)
        return (
            list(unique_results.values()),
            request_failed,
        )

    def search_candidates(self, path, language, query=""):
        """Return aggregated candidates for a non-interactive caller."""
        results, request_failed = self._gather_candidates(
            path,
            language,
            query,
        )
        if not results and request_failed:
            raise RuntimeError("OpenSubtitles search request failed")
        return results

    def get_download_link(self, selected_subtitles):
        url = "https://api.opensubtitles.com/api/v1/download"

        def build_headers():
            return {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Api-Key": self.api_key,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": self.user_agent,
            }

        try:
            file_id = int(selected_subtitles["attributes"]["files"][0]["file_id"])
        except (KeyError, TypeError, IndexError, ValueError) as e:
            raise RuntimeError(
                "OpenSubtitles result has no usable file_id "
                f"({type(e).__name__}: {e}); try the next candidate"
            ) from e
        payload = {"file_id": file_id}
        try:
            response = requests.post(
                url, headers=build_headers(), data=json.dumps(payload), timeout=10
            )
            if response.status_code == 401 and self.username and self.password:
                # The cached token is the usual suspect (expired/revoked):
                # fetch one fresh token and retry the same file_id once.
                refreshed = self._fresh_login()
                if refreshed:
                    self.token = refreshed
                    response = requests.post(
                        url,
                        headers=build_headers(),
                        data=json.dumps(payload),
                        timeout=10,
                    )
            response.raise_for_status()
            link = response.json().get("link")
            if not link:
                raise RuntimeError(
                    "OpenSubtitles accepted the request but returned no "
                    "download link; try the next candidate"
                )
            return link
        except requests.exceptions.HTTPError as e:
            status = (
                e.response.status_code if getattr(e, "response", None) else "unknown"
            )
            detail = ""
            try:
                body = e.response.text if getattr(e, "response", None) else ""
                detail = f": {(body or '')[:200]}".rstrip()
            except Exception:
                detail = ""
            if status == 401:
                raise RuntimeError(
                    "OpenSubtitles authentication failed (401). Check the "
                    f"username/password/API key{detail}"
                ) from e
            if status == 403:
                raise RuntimeError(
                    "OpenSubtitles refused the download (403). The account "
                    f"may lack download rights{detail}"
                ) from e
            if status == 429:
                raise RuntimeError(
                    "OpenSubtitles download limit reached (429). Daily quota "
                    "is exhausted — try again tomorrow or use SubDL/SubSource"
                    f"{detail}"
                ) from e
            raise RuntimeError(
                f"OpenSubtitles download request failed ({status}){detail}"
            ) from e
        except requests.exceptions.RequestException as e:
            self.console.print(
                f"[bold red]Error during OpenSubtitles download link retrieval: {e}[/]"
            )
            raise RuntimeError(f"OpenSubtitles download request failed: {e}") from e
        except (KeyError, json.decoder.JSONDecodeError, TypeError, IndexError) as e:
            self.console.print(
                f"[bold red]Error parsing OpenSubtitles download link response: {e}[/]"
            )
            raise RuntimeError(
                "OpenSubtitles returned an unreadable download response "
                f"({type(e).__name__}); try the next candidate"
            ) from e
        except RuntimeError:
            raise
        except Exception as e:
            self.console.print(
                f"[bold red]Unexpected error during download link retrieval: {e}[/]"
            )
            raise RuntimeError(f"OpenSubtitles download failed: {e}") from e

    def save_subtitle(self, url, path):
        """Download and save subtitle file from url to path"""
        try:
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()
            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except requests.exceptions.RequestException as e:
            self.console.print(f"[bold red]Error downloading subtitle: {e}[/]")
            return False
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error saving subtitle: {e}[/]")
            return False

    def process_media_file(self, media_path, language_choice, media_name=""):
        try:
            path = Path(media_path)
            if not media_name:
                media_name = path.stem
            rprint(
                "[cyan]Searching for subtitles for[/cyan] "
                f"[yellow]{media_name}[/yellow]"
            )
            # Legacy OpenSubtitles always writes SRT here; this check must match the
            # writer below, not the TUI's format-preserving path.
            subtitle_path = self._output_path(
                path,
                f"{path.stem}.{language_choice}.srt",
            )
            if report_existing_subtitle(subtitle_path, self.console):
                return False
            results, _request_failed = self._gather_candidates(
                path,
                language_choice,
                media_name,
            )
            if not results:
                rprint(f"[red]No subtitles found for {media_name}[/red]")
                return False
            rprint(
                f"[green]Total unique results after all searches: "
                f"{len(results)}[/green]"
            )

            sorted_results = self.subtitle_utils.sort_list_of_dicts_by_key(
                results, "download_count"
            )

            if self.auto_select:
                selected_sub = self.subtitle_utils.auto_select_subtitle(
                    media_name, sorted_results
                )
            else:
                selected_sub = self.subtitle_utils.manual_select_subtitle(
                    media_name, sorted_results
                )

            if selected_sub is None:
                rprint("[yellow]Subtitle download cancelled.[/yellow]")
                return False

            download_link = self.get_download_link(selected_sub)
            if download_link is None:
                return False

            rprint(
                f"[green]>> Downloading {language_choice} subtitles for "
                f"{media_path}[/green]"
            )
            self.print_subtitle_info(selected_sub)
            if not self.save_subtitle(download_link, subtitle_path):
                return False
            self.subtitle_utils.clean_subtitles(subtitle_path)
            if self.sync_audio_to_subs == "ask":
                should_sync = self.subtitle_utils.ask_sync_subtitles()
                if should_sync:
                    self.subtitle_utils.sync_subtitles(media_path, subtitle_path)
            elif self.sync_audio_to_subs:
                self.subtitle_utils.sync_subtitles(media_path, subtitle_path)
            return True
        except Exception as e:
            self.console.print(
                f"[bold red]Unexpected error processing media file: {e}[/]"
            )
            return False

    def process_media_list(self, media_path_list, language_choice):
        for media_path in media_path_list:
            try:
                path = Path(media_path)
                if path.is_dir():
                    for file in path.iterdir():
                        if self.subtitle_utils.check_if_media_file(file):
                            result = self.process_media_file(file, language_choice)
                            if not result:
                                self.console.print(
                                    "[bold yellow]Warning: Could not find "
                                    f"subtitles for {file}[/]"
                                )
                elif self.subtitle_utils.check_if_media_file(path):
                    result = self.process_media_file(path, language_choice)
                    if not result:
                        self.console.print(
                            "[bold yellow]Warning: Could not find subtitles "
                            f"for {path}[/]"
                        )
            except Exception as e:
                self.console.print(
                    "[bold red]Unexpected error processing media list item "
                    f"{media_path}: {e}[/]"
                )

    def print_subtitle_info(self, sub):
        try:
            attrs = sub["attributes"]
            movie_name = attrs["feature_details"]["movie_name"]

            info_table = Table(title="Selected Subtitle Information", show_header=False)
            info_table.add_column("Property", style="cyan")
            info_table.add_column("Value", style="yellow")

            info_table.add_row("Movie Name", movie_name)
            info_table.add_row("Subtitle ID", sub["id"])
            info_table.add_row("File ID", str(attrs["files"][0]["file_id"]))
            info_table.add_row("Language", attrs["language"])
            info_table.add_row("Release", attrs["release"])
            info_table.add_row("Downloads", str(attrs["download_count"]))
            info_table.add_row(
                "AI Translated", "Yes" if attrs["ai_translated"] else "No"
            )
            info_table.add_row(
                "Machine Translated", "Yes" if attrs["machine_translated"] else "No"
            )
            info_table.add_row(
                "Hash Match", "Yes" if attrs.get("moviehash_match", False) else "No"
            )
            info_table.add_row("URL", attrs["url"])

            self.console.print(info_table)
        except (KeyError, TypeError, IndexError) as e:
            self.console.print(f"[bold red]Error printing subtitle information: {e}[/]")
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error: {e}[/]")


if __name__ == "__main__":
    print("This is a module, import it in your project")
