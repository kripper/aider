#!/usr/bin/env python

import sys
import re
import readline
import traceback
import argparse
from rich.console import Console
from rich.prompt import Confirm, Prompt
from colorama import Fore, Style
from rich.live import Live
from rich.text import Text
from rich.markdown import Markdown
from dotenv import load_dotenv

from tqdm import tqdm

from pathlib import Path
from findblock import replace_most_similar_chunk

import os
import git
import openai

from dump import dump

import prompts

history_file = ".coder.history"
try:
    readline.read_history_file(history_file)
except FileNotFoundError:
    pass

openai.api_key = os.getenv("OPENAI_API_KEY")


class Coder:
    fnames = dict()
    last_modified = 0
    repo = None

    def __init__(self, main_model, files, pretty):
        self.main_model = main_model

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=True, no_color=True)

        for fname in files:
            fname = Path(fname)
            if not fname.exists():
                self.console.print(f"[red]Creating {fname}")
                fname.touch()
            else:
                self.console.print(f"[red]Loading {fname}")

            self.fnames[str(fname)] = fname.stat().st_mtime

        self.set_repo()
        if not self.repo:
            self.console.print(
                "[red bold]Will not automatically commit edits as they happen."
            )

        self.pretty = pretty

    def set_repo(self):
        repo_paths = list(
            git.Repo(fname, search_parent_directories=True).git_dir
            for fname in self.fnames
        )
        num_repos = len(set(repo_paths))

        if num_repos == 0:
            self.console.print("[red bold]Files are not in a git repo.")
            return
        if num_repos > 1:
            self.console.print("[red bold]Files are in different git repos.")
            return

        repo = git.Repo(repo_paths.pop())

        new_files = []
        for fname in self.fnames:
            relative_fname = os.path.relpath(fname, repo.working_tree_dir)
            tracked_files = set(repo.git.ls_files().splitlines())
            if relative_fname not in tracked_files:
                new_files.append(relative_fname)

        if new_files:
            self.console.print(f"[red bold]Files not tracked in {repo.git_dir}:")
            for fn in new_files:
                self.console.print(f"[red bold]  {fn}")
            if Confirm.ask("[bold red]Add them?", console=self.console):
                for relative_fname in new_files:
                    repo.git.add(relative_fname)
                    self.console.print(
                        f"[red bold]Added {relative_fname} to the git repo"
                    )
                commit_message = "Initial commit: Added new files to the git repo."
                repo.git.commit("-m", commit_message, "--no-verify")
                self.console.print(
                    f"[green bold]Committed new files with message: {commit_message}"
                )
            else:
                self.console.print(
                    "[red bold]Skipped adding new files to the git repo."
                )
                return

        self.repo = repo

    def quoted_file(self, fname):
        prompt = "\n"
        prompt += fname
        prompt += "\n```\n"
        prompt += Path(fname).read_text()
        prompt += "\n```\n"
        return prompt

    def get_files_content(self):
        prompt = ""
        for fname in self.fnames:
            prompt += self.quoted_file(fname)
        return prompt

    def get_input(self):
        if self.pretty:
            self.console.rule()
        else:
            print()

        inp = ""
        multiline_input = False
        if self.pretty:
            print(Fore.GREEN, end="\r")
        else:
            print()

        while True:
            try:
                if multiline_input:
                    line = input(". ")
                else:
                    line = input("> ")
            except EOFError:
                return

            if line.strip() == "{" and not multiline_input:
                multiline_input = True
                continue
            elif line.strip() == "}" and multiline_input:
                break
            elif multiline_input:
                inp += line + "\n"
            else:
                inp = line
                break

        if self.pretty:
            print(Style.RESET_ALL)
        else:
            print()

        readline.write_history_file(history_file)
        return inp

    def get_last_modified(self):
        return max(Path(fname).stat().st_mtime for fname in self.fnames)

    def get_files_messages(self):
        files_content = prompts.files_content_prefix
        files_content += self.get_files_content()

        files_messages = [
            dict(role="user", content=files_content),
            dict(role="assistant", content="Ok."),
            dict(
                role="system",
                content=prompts.files_content_suffix + prompts.system_reminder,
            ),
        ]

        return files_messages

    def run(self):
        self.done_messages = []
        self.cur_messages = []

        self.num_control_c = 0

        while True:
            try:
                self.run_loop()
            except KeyboardInterrupt:
                self.num_control_c += 1
                if self.num_control_c >= 2:
                    break
                self.console.print("[bold red]^C again to quit")

        if self.pretty:
            print(Style.RESET_ALL)

    def run_loop(self):
        inp = self.get_input()
        if inp is None:
            return

        self.num_control_c = 0

        if self.last_modified < self.get_last_modified():
            self.commit(ask=True)

            # files changed, move cur messages back behind the files messages
            self.done_messages += self.cur_messages
            self.done_messages += [
                dict(role="user", content=prompts.files_content_local_edits),
                dict(role="assistant", content="Ok."),
            ]
            self.cur_messages = []

        self.cur_messages += [
            dict(role="user", content=inp),
        ]

        messages = [
            dict(role="system", content=prompts.main_system + prompts.system_reminder),
        ]
        messages += self.done_messages
        messages += self.get_files_messages()
        messages += self.cur_messages

        # self.show_messages(messages, "all")

        content, interrupted = self.send(messages)
        if interrupted:
            content += "\n^C KeyboardInterrupt"

        Path("tmp.last-edit.md").write_text(content)

        self.cur_messages += [
            dict(role="assistant", content=content),
        ]

        self.console.print()
        if interrupted:
            return True

        try:
            edited = self.update_files(content, inp)
        except Exception as err:
            print(err)
            print()
            traceback.print_exc()
            edited = None

        if not edited:
            return True

        res = self.commit(history=self.cur_messages)
        if res:
            commit_hash, commit_message = res

            saved_message = prompts.files_content_gpt_edits.format(
                hash=commit_hash,
                message=commit_message,
            )
        else:
            self.console.print("[red bold]No changes found in tracked files.")
            saved_message = prompts.files_content_gpt_no_edits

        self.check_for_local_edits(True)
        self.done_messages += self.cur_messages
        self.done_messages += [
            dict(role="user", content=saved_message),
            dict(role="assistant", content="Ok."),
        ]
        self.cur_messages = []
        return True

    def show_messages(self, messages, title):
        print(title.upper(), "*" * 50)

        for msg in messages:
            print()
            print("-" * 50)
            role = msg["role"].upper()
            content = msg["content"].splitlines()
            for line in content:
                print(role, line)

    def send(self, messages, model=None, progress_bar_expected=0, silent=False):
        # self.show_messages(messages, "all")

        if not model:
            model = self.main_model

        import time
        from openai.error import RateLimitError

        while True:
            try:
                completion = openai.ChatCompletion.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    stream=True,
                )
                break
            except RateLimitError as e:
                retry_after = e.retry_after
                print(f"Rate limit exceeded. Retrying in {retry_after} seconds.")
                time.sleep(retry_after)

        interrupted = False
        try:
            if progress_bar_expected:
                self.show_send_progress(completion, progress_bar_expected)
            elif self.pretty and not silent:
                self.show_send_output_color(completion)
            else:
                self.show_send_output_plain(completion, silent)
        except KeyboardInterrupt:
            interrupted = True

        return self.resp, interrupted

    def show_send_progress(self, completion, progress_bar_expected):
        self.resp = ""
        pbar = tqdm(total=progress_bar_expected)
        for chunk in completion:
            try:
                text = chunk.choices[0].delta.content
                self.resp += text
            except AttributeError:
                continue

            pbar.update(len(text))

        pbar.update(progress_bar_expected)
        pbar.close()

    def show_send_output_plain(self, completion, silent):
        self.resp = ""

        for chunk in completion:
            if chunk.choices[0].finish_reason not in (None, "stop"):
                dump(chunk.choices[0].finish_reason)
            try:
                text = chunk.choices[0].delta.content
                self.resp += text
            except AttributeError:
                continue

            if not silent:
                sys.stdout.write(text)
                sys.stdout.flush()

    def show_send_output_color(self, completion):
        self.resp = ""

        with Live(vertical_overflow="scroll") as live:
            for chunk in completion:
                if chunk.choices[0].finish_reason not in (None, "stop"):
                    assert False, "Exceeded context window!"
                try:
                    text = chunk.choices[0].delta.content
                    self.resp += text
                except AttributeError:
                    continue

                md = Markdown(self.resp, style="blue", code_theme="default")
                live.update(md)

            live.update(Text(""))
            live.stop()

        md = Markdown(self.resp, style="blue", code_theme="default")
        self.console.print(md)

    pattern = re.compile(
        r"(^```\S*\s*)?^((?:[a-zA-Z]:\\|/)?(?:[\w\s.-]+[\\/])*\w+(\.\w+)?)\s+(^```\S*\s*)?^<<<<<<< ORIGINAL\n(.*?\n?)^=======\n(.*?)^>>>>>>> UPDATED",  # noqa: E501
        re.MULTILINE | re.DOTALL,
    )

    def update_files(self, content, inp):
        edited = set()
        for match in self.pattern.finditer(content):
            _, path, _, _, original, updated = match.groups()

            if path not in self.fnames:
                if not Path(path).exists():
                    question = f"[red bold]Allow creation of new file {path}?"
                else:
                    question = f"[red bold]Allow edits to {path} which was not previously provided?"
                if not Confirm.ask(question, console=self.console):
                    self.console.print(f"[red]Skipping edit to {path}")
                    continue

                self.fnames[path] = 0

            edited.add(path)
            if self.do_replace(path, original, updated):
                continue
            edit = match.group()
            self.do_gpt_powered_replace(path, edit, inp)

        return edited

    def do_replace(self, fname, before_text, after_text):
        before_text = self.strip_quoted_wrapping(before_text, fname)
        after_text = self.strip_quoted_wrapping(after_text, fname)

        fname = Path(fname)

        # does it want to make a new file?
        if not fname.exists() and not before_text:
            print("Creating empty file:", fname)
            fname.touch()

        content = fname.read_text()

        if not before_text and not content:
            # first populating an empty file
            new_content = after_text
        else:
            new_content = replace_most_similar_chunk(content, before_text, after_text)
            if not new_content:
                return

        fname.write_text(new_content)
        self.console.print(f"[red]Applied edit to {fname}")
        return True

    def do_gpt_powered_replace(self, fname, edit, request):
        model = "gpt-3.5-turbo"
        print(f"Asking {model} to apply ambiguous edit to {fname}...")

        fname = Path(fname)
        content = fname.read_text()
        prompt = prompts.editor_user.format(
            request=request,
            edit=edit,
            fname=fname,
            content=content,
        )

        messages = [
            dict(role="system", content=prompts.editor_system),
            dict(role="user", content=prompt),
        ]
        res, interrupted = self.send(
            messages, progress_bar_expected=len(content) + len(edit) / 2, model=model
        )
        if interrupted:
            return

        res = self.strip_quoted_wrapping(res, fname)
        fname.write_text(res)

    def strip_quoted_wrapping(self, res, fname=None):
        if not res:
            return res

        res = res.splitlines()

        if fname and res[0].strip().endswith(Path(fname).name):
            res = res[1:]

        if res[0].startswith("```") and res[-1].startswith("```"):
            res = res[1:-1]

        res = "\n".join(res)
        if res and res[-1] != "\n":
            res += "\n"

        return res

    def commit(self, history=None, prefix=None, ask=False):
        repo = self.repo
        if not repo:
            return

        if not repo.is_dirty():
            return

        diffs = ""
        dirty_fnames = []
        relative_dirty_fnames = []
        for fname in self.fnames:
            relative_fname = os.path.relpath(fname, repo.working_tree_dir)
            these_diffs = repo.git.diff("HEAD", relative_fname)
            if these_diffs:
                dirty_fnames.append(fname)
                relative_dirty_fnames.append(relative_fname)
                diffs += these_diffs + "\n"

        if not dirty_fnames:
            self.last_modified = self.get_last_modified()
            return

        self.console.print(Text(diffs))

        diffs = "# Diffs:\n" + diffs

        # for fname in dirty_fnames:
        #    self.console.print(f"[red]  {fname}")

        context = ""
        if history:
            context += "# Context:\n"
            for msg in history:
                context += msg["role"].upper() + ": " + msg["content"] + "\n"

        messages = [
            dict(role="system", content=prompts.commit_system),
            dict(role="user", content=context + diffs),
        ]

        # if history:
        #    self.show_messages(messages, "commit")

        commit_message, interrupted = self.send(
            messages,
            model="gpt-3.5-turbo",
            silent=True,
        )

        commit_message = commit_message.strip().strip('"').strip()

        if interrupted:
            raise KeyboardInterrupt

        if prefix:
            commit_message = prefix + commit_message

        if ask:
            self.last_modified = self.get_last_modified()

            self.console.print("[red]Files have uncommitted changes.\n")
            self.console.print(f"[red]Suggested commit message:\n{commit_message}\n")

            res = Prompt.ask(
                "[red]Commit before the chat proceeds? \[y/n/commit message]",  # noqa: W605
                console=self.console,
            ).strip()
            if res.lower() in ["n", "no"]:
                self.console.print("[red]Skipped commmit.")
                return
            if res.lower() not in ["y", "yes"]:
                commit_message = res

        repo.git.add(*relative_dirty_fnames)

        full_commit_message = commit_message + "\n\n" + context
        repo.git.commit("-m", full_commit_message, "--no-verify")
        commit_hash = repo.head.commit.hexsha[:7]
        self.console.print(f"[green]{commit_hash} {commit_message}")

        self.last_modified = self.get_last_modified()

        return commit_hash, commit_message


def main():
    load_dotenv()
    env_prefix = "CODER_"
    parser = argparse.ArgumentParser(description="Chat with GPT about code")
    parser.add_argument(
        "files", metavar="FILE", nargs="+", help="a list of source code files",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="gpt-4",
        help="Specify the model to use for the main chat (default: gpt-4)",
    )
    parser.add_argument(
        "-3",
        action="store_const",
        dest="model",
        const="gpt-3.5-turbo",
        help="Use gpt-3.5-turbo model for the main chat",
    )
    parser.add_argument(
        "-4",
        action="store_const",
        dest="model",
        const="gpt-4",
        help="Use gpt-4 model for the main chat",
    )
    parser.add_argument(
        "--no-pretty",
        action="store_false",
        dest="pretty",
        help="Disable prettyd output of GPT responses",
        default=bool(int(os.environ.get(env_prefix + "PRETTY", 1)))
    )
    parser.add_argument(
        "--apply",
        metavar="FILE",
        help="Apply the changes from the given file instead of running the chat",
    )
    parser.add_argument(
        "--commit-dirty",
        action="store_true",
        help="Commit dirty files without confirmation",
        default=bool(int(os.environ.get(env_prefix + "COMMIT_DIRTY", 0)))
    )
    args = parser.parse_args()

    fnames = args.files
    pretty = args.pretty

    coder = Coder(args.model, fnames, pretty)
    coder.commit(ask=not args.commit_dirty, prefix="WIP: ")

    if args.apply:
        with open(args.apply, "r") as f:
            content = f.read()
        coder.update_files(content, inp="")
        return

    coder.run()


if __name__ == "__main__":
    status = main()
    sys.exit(status)
