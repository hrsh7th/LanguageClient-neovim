import neovim
import os
import subprocess
import json
import inspect
import threading
from functools import partial
from typing import List, Dict, Union, Any  # noqa: F401

from . util import (
    getRootPath, pathToURI, uriToPath, escape,
    getGotoFileCommand, getCommandUpdateSigns)
from . logger import logger
from . RPC import RPC
from . TextDocumentItem import TextDocumentItem
from . DiagnosticsDisplay import DiagnosticsDisplay
from . Sign import Sign
import re


def args(warn=True):
    def wrapper(f):
        def wrappedf(*args, **kwargs):
            self = args[0]
            languageId, = self.getArgs(["languageId"], [], {})
            if not self.alive(languageId, warn):
                return

            argspec = inspect.getfullargspec(f)
            fullargs = self.getArgs(argspec.args, args, kwargs)
            return f(*fullargs)
        return wrappedf
    return wrapper


def convert_lsp_completion_item_to_vim_style(item):
    e = {}
    e['icase'] = 1
    # insertText:
    # A string that should be inserted a document when selecting
    # this completion. When `falsy` the label is used.
    e['word'] = item.get('insertText', "") or item['label']
    e['abbr'] = item['label']
    e['dup'] = 1
    e['menu'] = item.get('detail', "")
    e['info'] = item.get('documentation', "")
    return e


@neovim.plugin
class LanguageClient:
    _instance = None  # type: LanguageClient

    def __init__(self, nvim):
        logger.info('__init__')
        type(self)._instance = self
        self.nvim = nvim
        self.server = {}  # type: Dict[str, subprocess.Popen]
        self.rpc = {}  # type: Dict[str, RPC]
        self.capabilities = {}
        self.rootUri = None
        self.textDocuments = {}  # type: Dict[str, TextDocumentItem]
        self.diagnostics = {}
        self.lastLine = -1
        self.hlsid = None
        self.signs = []
        self.serverCommands = {}
        self.changeThreshold = 0
        self.trace = "off"  # trace settings passed to server
        self.autoStart = self.nvim.vars.get(
            "LanguageClient_autoStart", False)

    def asyncCommand(self, cmds: str) -> None:
        self.nvim.async_call(self.nvim.command, cmds)

    def asyncEcho(self, message: str) -> None:
        message = escape(message)
        self.asyncCommand("echo '{}'".format(message))

    def asyncEchomsg(self, message: str) -> None:
        message = escape(message)
        self.asyncCommand("echomsg '{}'".format(message))

    def asyncEchoEllipsis(self, msg: str, columns: int):
        """
        Print as much of msg as possible without trigging "Press Enter"
        prompt.

        Inspired by neomake, which is in turn inspired by syntastic.
        """
        msg = msg.replace("\n", ". ")
        if len(msg) > columns - 12:
            msg = msg[:columns - 15] + "..."

        self.asyncEcho(msg)

    def setloclist(self, loclist: list) -> None:
        windownumber = self.nvim.current.window.number
        self.nvim.funcs.setloclist(windownumber, loclist)

    def getArgs(self, keys: List, args: List, kwargs: Dict) -> List:
        res = {}  # type: Dict[str, Any]
        for k in keys:
            res[k] = None
        if len(args) > 1 and len(args[1]) > 0:  # from vimscript side
            res.update(args[1][0])
        else:  # python side
            res.update(kwargs)

        cursor = []  # type: List[int]

        for k in keys:
            if res[k] is not None:
                continue
            elif k == "languageId":
                res[k] = self.nvim.current.buffer.options["filetype"]
            elif k == "self":
                res[k] = self
            elif k == "uri":
                res[k] = pathToURI(self.nvim.current.buffer.name)
            elif k == "line":
                cursor = self.nvim.current.window.cursor
                res[k] = cursor[0] - 1
            elif k == "character":
                res[k] = cursor[1]
            elif k == "cword":
                res[k] = self.nvim.funcs.expand("<cword>")
            elif k == "bufnames":
                res[k] = [b.name for b in self.nvim.buffers]
            elif k == "columns":
                res[k] = self.nvim.options["columns"]

        return [res[k] for k in keys]

    def applyChanges(
            self, changes: Dict,
            curPos: Dict, bufnames: List) -> None:
        cmd = "echo ''"
        for uri, edits in changes.items():
            path = uriToPath(uri)
            cmd += "| " + getGotoFileCommand(path, bufnames)
            for edit in edits:
                line = edit['range']['start']['line'] + 1
                character = edit['range']['start']['character'] + 1
                newText = edit['newText']
                cmd += "| execute 'normal! {}G{}|cw{}'".format(
                    line, character, newText)
        cmd += "| buffer {} | normal! {}G{}|".format(
            uriToPath(curPos["uri"]),
            curPos["line"] + 1,
            curPos["character"] + 1)
        self.asyncCommand(cmd)

    @neovim.function("LanguageClient_alive", sync=True)
    def alive_wrapper(self, args: List):
        languageId, = self.getArgs(["languageId"], [self, args], {})
        return self.alive(languageId, False)

    def alive(self, languageId, warn) -> bool:
        ret = True
        if languageId not in self.server:
            ret = False
            msg = "Language client is not running. Try :LanguageClientStart"
        elif self.server[languageId].poll() is not None:
            ret = False
            msg = "Failed to start language server: {}".format(
                self.server[languageId].stderr.readlines())
            logger.error(msg)

        if ret is False and warn:
            self.asyncEcho(msg)
        return ret

    @neovim.function("LanguageClient_setLoggingLevel")
    def setLoggingLevel(self, args):
        logger.setLevel({
            "ERROR": 40,
            "WARNING": 30,
            "INFO": 20,
            "DEBUG": 10,
        }[args[0]])

    def defineSigns(self) -> None:
        diagnosticsDisplay = self.nvim.eval(
            "get(g:, 'LanguageClient_diagnosticsDisplay', {})")
        DiagnosticsDisplay.update(diagnosticsDisplay)
        cmd = "echo ''"
        for level in DiagnosticsDisplay.values():
            name = level["name"]
            signText = level["signText"]
            signTexthl = level["signTexthl"]
            cmd += ("| execute 'sign define LanguageClient{}"
                    " text={} texthl={}'").format(name, signText, signTexthl)
        cmd += ("| execute 'sign define LanguageClientDummy'")
        self.asyncCommand(cmd)

    @neovim.function("LanguageClient_registerServerCommands")
    def registerServerCommands(self, args: List) -> None:
        """
        Add or update serverCommands.
        """
        serverCommands = args[0]  # Dict[str, str]
        self.serverCommands.update(serverCommands)

    @neovim.command('LanguageClientStart')
    def start(self, warn=True) -> None:
        # Sync settings.
        self.serverCommands.update(self.nvim.vars.get(
            "LanguageClient_serverCommands", {}))
        self.changeThreshold = self.nvim.vars.get(
            "LanguageClient_changeThreshold", 0)
        self.selectionUI = self.nvim.vars.get(
            "LanguageClient_selectionUI")
        self.trace = self.nvim.vars.get(
            "LanguageClient_trace", "off")
        if not self.selectionUI:
            if self.nvim.vars.get('loaded_fzf') == 1:
                self.selectionUI = "fzf"
            else:
                self.selectionUI = "location-list"

        languageId, = self.getArgs(["languageId"], [], {})
        if self.alive(languageId, False):
            self.asyncEcho("Language client has already started.")
            return

        if languageId not in self.serverCommands:
            if not warn:
                return
            msg = "No language server commmand found for type: {}.".format(
                languageId)
            logger.error(msg)
            self.asyncEcho(msg)
            return

        logger.info('Begin LanguageClientStart')

        self.languageId = languageId
        command = self.serverCommands[languageId]
        command = [os.path.expandvars(os.path.expanduser(cmd))
                   for cmd in command]

        try:
            self.server[languageId] = subprocess.Popen(
                # ["/bin/bash", "/tmp/wrapper.sh"],
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except Exception as ex:
            msg = "Failed to start language server: " + ex.args[1]
            logger.exception(msg)
            self.asyncEchomsg(msg)
            return

        self.rpc[languageId] = RPC(
            self.server[languageId].stdout, self.server[languageId].stdin,
            self.handleRequestOrNotification,
            self.handleRequestOrNotification)
        threading.Thread(
            target=self.rpc[languageId].serve,
            name="RPC Server",
            daemon=True).start()

        if len(self.server) == 1:
            self.defineSigns()

        logger.info('End LanguageClientStart')

        self.initialize(languageId=languageId)

    @neovim.command("LanguageClientStop")
    @args()
    def stop(self, languageId: str = None) -> None:
        self.rpc[languageId].run = False
        self.exit([{
            "languageId": languageId
        }])
        del self.server[languageId]

    @neovim.function('LanguageClient_initialize')
    @args()
    def initialize(
            self, rootPath: str = None, languageId: str = None,
            cbs: List = None) -> None:
        logger.info('Begin initialize')

        if rootPath is None:
            rootPath = getRootPath(self.nvim.current.buffer.name, languageId)
        logger.info("rootPath: " + rootPath)
        self.rootUri = pathToURI(rootPath)
        if cbs is None:
            cbs = [self.handleInitializeResponse, self.handleError]

        self.rpc[languageId].call('initialize', {
            "processId": os.getpid(),
            "rootPath": rootPath,
            "rootUri": self.rootUri,
            "capabilities": {},
            "trace": self.trace,
        }, cbs)

    def handleInitializeResponse(self, result: Dict) -> None:
        self.capabilities = result['capabilities']
        self.nvim.async_call(self.textDocument_didOpen)
        self.nvim.async_call(self.registerCMSource, result)
        logger.info('End initialize')

    def registerCMSource(self, result: Dict) -> None:
        completionProvider = result["capabilities"].get("completionProvider")
        if completionProvider is None:
            return

        trigger_patterns = []
        for c in completionProvider.get("triggerCharacters", []):
            trigger_patterns.append(re.escape(c) + '$')

        try:
            self.nvim.call('cm#register_source', dict(
                name='LanguageClient_%s' % self.languageId,
                priority=9,
                scopes=[self.languageId],
                cm_refresh_patterns=trigger_patterns,
                abbreviation='',
                cm_refresh='LanguageClient_completionManager_refresh'))
            logger.info("register completion manager source ok.")
        except Exception as ex:
            logger.warn("register completion manager source failed. Error: " +
                        repr(ex))

    @neovim.autocmd("BufReadPost", pattern="*")
    def handleBufReadPost(self):
        languageId, = self.getArgs(["languageId"], [], {})
        if self.alive(languageId, warn=False):
            self.textDocument_didOpen()
        elif self.autoStart:
            self.start(warn=False)

    @neovim.autocmd("VimEnter", pattern="*")
    def handleVimEnter(self):
        # Fix the issue that BufReadPost is not trigged using `nvim myfile`.
        self.nvim.async_call(self.handleBufReadPost)

    @args()
    def textDocument_didOpen(
            self, uri: str = None, languageId: str = None) -> None:
        # Keep sign column open.
        if self.nvim.vars.get("LanguageClient_signColumnAlwaysOn", True):
            bufnumber = self.nvim.current.buffer.number
            cmd = ("sign place 99999"
                   " line=99999 name=LanguageClientDummy"
                   " buffer={}").format(bufnumber)
            self.asyncCommand(cmd)

        logger.info('textDocument/didOpen')

        text = str.join("\n", self.nvim.current.buffer)

        textDocumentItem = TextDocumentItem(uri, languageId, text)
        self.textDocuments[uri] = textDocumentItem

        self.rpc[languageId].notify('textDocument/didOpen', {
            "textDocument": {
                "uri": textDocumentItem.uri,
                "languageId": textDocumentItem.languageId,
                "version": textDocumentItem.version,
                "text": textDocumentItem.text,
            }
        })

    @neovim.function('LanguageClient_textDocument_didClose')
    @args(warn=False)
    def textDocument_didClose(
            self, uri: str = None, languageId: str = None) -> None:
        logger.info('textDocument/didClose')

        del self.textDocuments[uri]

        self.rpc[languageId].notify('textDocument/didClose', {
            "textDocument": {
                "uri": uri
            }
        })

    @neovim.function('LanguageClient_textDocument_hover')
    @args()
    def textDocument_hover(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            cbs: List = None) -> None:
        logger.info('Begin textDocument/hover')

        self.textDocument_didChange()

        if cbs is None:
            cbs = [self.handleTextDocumentHoverResponse, self.handleError]

        self.rpc[languageId].call('textDocument/hover', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character
            }
        }, cbs)

    def markedStringToString(self, s: Any) -> str:
        if isinstance(s, str):
            return s
        else:
            return s["value"]

    def handleTextDocumentHoverResponse(self, result: Dict) -> None:
        contents = result.get("contents")
        if contents is None:
            contents = ""

        value = ""
        if isinstance(contents, list):
            for markedString in result['contents']:
                value += self.markedStringToString(markedString)
        else:
            value += self.markedStringToString(contents)
        self.asyncEcho(value)

        logger.info('End textDocument/hover')

    @neovim.function('LanguageClient_textDocument_definition')
    @args()
    def textDocument_definition(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            bufnames: str = None, cbs: List = None) -> None:
        logger.info('Begin textDocument/definition')

        self.textDocument_didChange()

        if cbs is None:
            cbs = [partial(self.handleTextDocumentDefinitionResponse,
                           bufnames=bufnames),
                   self.handleError]

        self.rpc[languageId].call('textDocument/definition', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character
            }
        }, cbs)

    def handleTextDocumentDefinitionResponse(
            self, result: List, bufnames: Union[List, Dict]) -> None:
        if isinstance(result, list) and len(result) > 1:
            # TODO
            msg = ("Handling multiple definitions is not implemented yet."
                   " Jumping to first.")
            logger.error(msg)
            self.asyncEcho(msg)

        if isinstance(result, list):
            if len(result) == 0:
                self.asyncEcho("Not found.")
                return
            defn = result[0]
        else:
            defn = result
        path = uriToPath(defn["uri"])
        cmd = getGotoFileCommand(path, bufnames)
        line = defn['range']['start']['line'] + 1
        character = defn['range']['start']['character'] + 1
        cmd += "| normal! {}G{}|".format(line, character)
        self.asyncCommand(cmd)

        logger.info('End textDocument/definition')

    @neovim.function('LanguageClient_textDocument_rename')
    @args()
    def textDocument_rename(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            cword: str = None, newName: str = None,
            bufnames: List[str] = None, cbs: List = None) -> None:
        logger.info('Begin textDocument/rename')

        self.textDocument_didChange()

        if newName is None:
            self.nvim.funcs.inputsave()
            newName = self.nvim.funcs.input("Rename to: ", cword)
            self.nvim.funcs.inputrestore()
        if cbs is None:
            cbs = [partial(self.handleTextDocumentRenameResponse,
                           curPos={
                               "line": line,
                               "character": character,
                               "uri": uri},
                           bufnames=bufnames),
                   self.handleError]

        self.rpc[languageId].call('textDocument/rename', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character,
            },
            "newName": newName
        }, cbs)

    def handleTextDocumentRenameResponse(
            self, result: Dict,
            curPos: Dict, bufnames: List) -> None:
        changes = result['changes']
        self.applyChanges(changes, curPos, bufnames)
        logger.info('End textDocument/rename')

    @neovim.function('LanguageClient_textDocument_documentSymbol')
    @args()
    def textDocument_documentSymbol(
            self, uri: str = None, languageId: str = None,
            sync: bool = None, cbs: List = None) -> None:
        logger.info('Begin textDocument/documentSymbol')

        self.textDocument_didChange()

        if not sync and not cbs:
            cbs = [partial(self.handleTextDocumentDocumentSymbolResponse,
                           uri=uri),
                   self.handleError]

        return self.rpc[languageId].call('textDocument/documentSymbol', {
            "textDocument": {
                "uri": uri
            }
        }, cbs)

    def fzf(self, source: List, sink: str) -> None:
        self.asyncCommand("""
call fzf#run(fzf#wrap({{
    'source': {},
    'sink': function('{}')
    }}))
""".replace("\n", "").format(json.dumps(source), sink))
        self.nvim.async_call(self.nvim.feedkeys, "i")

    def handleTextDocumentDocumentSymbolResponse(
            self, symbols: List, uri: str) -> None:
        if self.selectionUI == "fzf":
            source = []
            for sb in symbols:
                name = sb["name"]
                start = sb["location"]["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                entry = "{}:{}:\t{}".format(line, character, name)
                source.append(entry)
            self.fzf(source,
                     "LanguageClient#FZFSinkTextDocumentDocumentSymbol")
        elif self.selectionUI == "location-list":
            loclist = []
            path = uriToPath(uri)
            for sb in symbols:
                name = sb["name"]
                start = sb["location"]["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                loclist.append({
                    "filename": path,
                    "lnum": line,
                    "col": character,
                    "text": name,
                })
            self.nvim.async_call(self.setloclist, loclist)
            self.asyncEcho("Document symbols populated to location list.")
        else:
            msg = "No selection UI found. Consider install fzf or denite.vim."
            self.asyncEcho(msg)
            logger.warn(msg)

        logger.info('End textDocument/documentSymbol')

    @neovim.function('LanguageClient_FZFSinkTextDocumentDocumentSymbol')
    def fzfSinkTextDocumentDocumentSymbol(self, args: List) -> None:
        splitted = args[0].split(":")
        line = splitted[0]
        character = splitted[1]
        self.asyncCommand("normal! {}G{}|".format(line, character))

    @neovim.function('LanguageClient_workspace_symbol')
    @args()
    def workspace_symbol(
            self, languageId: str = None, query: str = None,
            sync: bool = None, cbs: List = None) -> None:
        logger.info("Begin workspace/symbol")

        if query is None:
            query = ""
        if not sync and not cbs:
            cbs = [self.handleWorkspaceSymbolResponse,
                   self.handleError]

        return self.rpc[languageId].call('workspace/symbol', {
            "query": query
        }, cbs)

    def handleWorkspaceSymbolResponse(self, symbols: list) -> None:
        if self.selectionUI == "fzf":
            source = []
            for sb in symbols:
                path = os.path.relpath(sb["location"]["uri"], self.rootUri)
                start = sb["location"]["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                name = sb["name"]
                entry = "{}:{}:{}\t{}".format(path, line, character, name)
                source.append(entry)
            self.fzf(source, "LanguageClient#FZFSinkWorkspaceSymbol")
        elif self.selectionUI == "location-list":
            loclist = []
            for sb in symbols:
                path = uriToPath(sb["location"]["uri"])
                start = sb["location"]["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                name = sb["name"]
                loclist.append({
                    "filename": path,
                    "lnum": line,
                    "col": character,
                    "text": name,
                })
            self.nvim.async_call(self.setloclist, loclist)
            self.asyncEcho("Workspace symbols populated to location list.")
        else:
            msg = "No selection UI found. Consider install fzf or denite.vim."
            self.asyncEcho(msg)
            logger.warn(msg)

        logger.info("End workspace/symbol")

    @neovim.function("LanguageClient_FZFSinkWorkspaceSymbol")
    def fzfSinkWorkspaceSymbol(self, args: List):
        bufnames, = self.getArgs(["bufnames"], [], {})

        splitted = args[0].split(":")
        path = uriToPath(os.path.join(self.rootUri, splitted[0]))
        line = splitted[1]
        character = splitted[2]

        cmd = getGotoFileCommand(path, bufnames)
        cmd += "| normal! {}G{}|".format(line, character)
        self.asyncCommand(cmd)

    @neovim.function('LanguageClient_textDocument_references')
    @args()
    def textDocument_references(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            sync: bool = None, cbs: List = None) -> None:
        logger.info("Begin textDocument/references")

        self.textDocument_didChange()

        if not sync and not cbs:
            cbs = [self.handleTextDocumentReferencesResponse,
                   self.handleError]

        return self.rpc[languageId].call('textDocument/references', {
            "textDocument": {
                "uri": uri,
            },
            "position": {
                "line": line,
                "character": character,
            },
            "context": {
                "includeDeclaration": True,
            },
        }, cbs)

    def handleTextDocumentReferencesResponse(self, locations: List) -> None:
        if self.selectionUI == "fzf":
            source = []  # type: List[str]
            for loc in locations:
                path = os.path.relpath(loc["uri"], self.rootUri)
                start = loc["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                entry = "{}:{}:{}".format(path, line, character)
                source.append(entry)
            self.fzf(source, "LanguageClient#FZFSinkTextDocumentReferences")
        elif self.selectionUI == "location-list":
            loclist = []
            for loc in locations:
                path = uriToPath(loc["uri"])
                start = loc["range"]["start"]
                line = start["line"] + 1
                character = start["character"] + 1
                loclist.append({
                    "filename": path,
                    "lnum": line,
                    "col": character
                })
            self.nvim.async_call(self.setloclist, loclist)
            self.asyncEcho("References populated to location list.")
        else:
            msg = "No selection UI found. Consider install fzf or denite.vim."
            self.asyncEcho(msg)
            logger.warn(msg)
        logger.info("End textDocument/references")

    @neovim.function("LanguageClient_FZFSinkTextDocumentReferences")
    def fzfSinkTextDocumentReferences(self, args: List) -> None:
        bufnames, = self.getArgs(["bufnames"], [], {})

        splitted = args[0].split(":")
        path = uriToPath(os.path.join(self.rootUri, splitted[0]))
        line = splitted[1]
        character = splitted[2]

        cmd = getGotoFileCommand(path, bufnames)
        cmd += "| normal! {}G{}|".format(line, character)
        self.asyncCommand(cmd)

    @neovim.autocmd("TextChanged", pattern="*")
    def textDocument_autocmdTextChanged(self):
        uri = pathToURI(self.nvim.current.buffer.name)
        if uri and uri in self.textDocuments:
            text_doc = self.textDocuments[uri]
            if text_doc.skip_change(self.changeThreshold):
                return
        self.textDocument_didChange()

    @neovim.autocmd("TextChangedI", pattern="*")
    def textDocument_autocmdTextChangedI(self):
        uri = pathToURI(self.nvim.current.buffer.name)
        if uri and uri in self.textDocuments:
            text_doc = self.textDocuments[uri]
            if text_doc.skip_change(self.changeThreshold):
                return
        self.textDocument_didChange()

    @args(warn=False)
    def textDocument_didChange(
            self, uri: str = None, languageId: str = None) -> None:
        logger.info("textDocument/didChange")

        if not uri or languageId not in self.serverCommands:
            return
        if uri not in self.textDocuments:
            self.textDocument_didOpen()
        newText = str.join("\n", self.nvim.current.buffer)
        text_doc = self.textDocuments[uri]
        if newText == text_doc.text:
            return

        version, changes = text_doc.change(newText)

        self.rpc[languageId].notify("textDocument/didChange", {
            "textDocument": {
                "uri": uri,
                "version": version
            },
            "contentChanges": changes
        })
        text_doc.commit_change()

    @neovim.autocmd("BufWritePost", pattern="*")
    @args(warn=False)
    def textDocument_didSave(
            self, uri: str = None, languageId: str = None) -> None:
        logger.info("textDocument/didSave")

        if languageId not in self.serverCommands:
            return

        self.rpc[languageId].notify("textDocument/didSave", {
            "textDocument": {
                "uri": uri
            }
        })

    @neovim.function("LanguageClient_textDocument_completion")
    @args(warn=False)
    def textDocument_completion(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            cbs: List = None) -> List:
        logger.info("Begin textDocument/completion")

        self.textDocument_didChange()

        items = self.rpc[languageId].call('textDocument/completion', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character
            }
        }, cbs)

        if items is None:
            items = []   # timeout

        if isinstance(items, dict):  # CompletionList object
            items = items["items"]

        return items

    @neovim.function("LanguageClient_textDocument_completionOmnifunc")
    @args(warn=False)
    def textDocument_completionOmnifunc(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            completeFromColumn: int = None,
            cbs: List = None) -> List:
        logger.info("Begin textDocument/completionOmnifunc")

        self.textDocument_didChange()

        items = self.rpc[languageId].call('textDocument/completion', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character
            }
        }, cbs)

        if items is None:
            items = []   # timeout

        if isinstance(items, dict):  # CompletionList object
            items = items["items"]

        matches = [convert_lsp_completion_item_to_vim_style(item)
                   for item in items]

        self.nvim.call('complete', completeFromColumn, matches)

    # this method is called by nvim-completion-manager framework
    @neovim.function("LanguageClient_completionManager_refresh")
    def completionManager_refresh(self, args) -> None:
        languageId, = self.getArgs(["languageId"], [], {})
        if not self.alive(languageId, False):
            return
        logger.info("completionManager_refresh: %s", args)
        info = args[0]
        ctx = args[1]

        if ctx["typed"] == "":
            return

        args = {}
        args["line"] = ctx["lnum"] - 1
        args["character"] = ctx["col"] - 1

        uri, languageId, line, character = self.getArgs(
            ["uri", "languageId", "line", "character"],
            [], {})

        logger.info("uri[%s] line[%s] character[%s]", uri, line, character)

        def cb(result):
            logger.info("result: %s", result)
            items = result
            isIncomplete = False
            if isinstance(result, dict):
                items = result["items"]
                isIncomplete = result.get('isIncomplete', False)

            # convert to vim style completion-items
            matches = [convert_lsp_completion_item_to_vim_style(item)
                       for item in items]

            self.nvim.call('cm#complete', info['name'], ctx,
                           ctx['startcol'], matches, isIncomplete, async=True)

        # Make sure the changing is synced.  Since `TextChangedI` will not be
        # triggered when popup menu is visible and neovim python client use
        # greenlet coroutine to handle rpc request/notification.
        self.textDocument_didChange()

        cbs = [cb, self.handleError]

        self.rpc[languageId].call('textDocument/completion', {
            "textDocument": {
                "uri": uri
            },
            "position": {
                "line": line,
                "character": character
            }
        }, cbs)

    @neovim.function("LanguageClient_exit")
    @args()
    def exit(self, languageId: str = None) -> None:
        logger.info("exit")

        self.rpc[languageId].notify("exit", {})

    def textDocument_publishDiagnostics(self, params) -> None:
        uri = params["uri"]
        diagnostics = {}
        for entry in params['diagnostics']:
            line = entry["range"]["start"]["line"]
            diagnostics[line] = entry
        self.diagnostics[uri] = diagnostics
        self.nvim.async_call(self.addHighlightAndSign, params)

    def addHighlightAndSign(self, params):
        path = uriToPath(params["uri"])
        buf = self.nvim.current.buffer
        if path != buf.name:
            return

        if not self.hlsid:
            self.hlsid = self.nvim.new_highlight_source()
        buf.clear_highlight(self.hlsid)
        bufnumber = buf.number
        signs = []
        qflist = []
        for entry in params["diagnostics"]:
            startline = entry["range"]["start"]["line"]
            startcharacter = entry["range"]["start"]["character"]
            endcharacter = entry["range"]["end"]["character"]
            severity = entry.get("severity", 3)
            display = DiagnosticsDisplay[severity]
            texthl = display["texthl"]
            buf.add_highlight(texthl, startline,
                              startcharacter, endcharacter, self.hlsid)

            signname = display["name"]

            signs.append(Sign(startline + 1, signname, bufnumber))

            qftype = {
                1: "E",
                2: "W",
                3: "I",
                4: "H",
            }[severity]
            qflist.append({
                "filename": path,
                "lnum": startline + 1,
                "col": startcharacter + 1,
                "nr": entry.get("code"),
                "text": entry["message"],
                "type": qftype,
            })

        cmd = getCommandUpdateSigns(self.signs, signs)
        self.signs = signs
        self.asyncCommand(cmd)

        self.nvim.funcs.setqflist(qflist)

    @neovim.autocmd("CursorMoved", pattern="*", eval="line('.')")
    def handleCursorMoved(self, line) -> None:
        if line == self.lastLine:
            return
        self.lastLine = line
        self.showDiagnosticMessage()

    @args(warn=False)
    def showDiagnosticMessage(self, uri: str, line: int, columns: int) -> None:
        entry = self.diagnostics.get(uri, {}).get(line)
        if not entry:
            self.asyncEcho("")
            return

        msg = ""
        if "severity" in entry:
            severity = {
                1: "E",
                2: "W",
                3: "I",
                4: "H",
            }[entry["severity"]]
            msg += "[{}]".format(severity)
        if "code" in entry:
            code = entry["code"]
            msg += str(code)
        msg += " " + entry["message"]

        self.asyncEchoEllipsis(msg, columns)

    @neovim.function("LanguageClient_completionItem/resolve")
    @args()
    def completionItem_resolve(
            self, completionItem: Dict = None,
            languageId: str = None, cbs: List = None) -> None:
        logger.info("Begin completionItem/resolve")
        if cbs is None:
            cbs = [self.handleCompletionItemResolveResponse,
                   self.handleError]

        self.rpc[languageId].call(
            "completionItem/resolve", completionItem, cbs)

    def handleCompletionItemResolveResponse(self, result):
        # TODO: proper integration.
        self.asyncEcho(json.dumps(result))
        logger.info("End completionItem/resolve")

    @neovim.function("LanguageClient_textDocument_signatureHelp")
    @args()
    def textDocument_signatureHelp(
            self, uri: str = None, languageId: str = None,
            line: int = None, character: int = None,
            cbs: List = None) -> None:
        logger.info("Begin textDocument/signatureHelp")

        self.textDocument_didChange()

        if cbs is None:
            cbs = [self.handleTextDocumentSignatureHelpResponse,
                   self.handleError]

        self.rpc[languageId].call("textDocument/signatureHelp", {
            "textDocument": uri,
            "position": {
                "line": line,
                "character": character,
            }
        }, cbs)

    def handleTextDocumentSignatureHelpResponse(self, result):
        # TODO: proper integration.
        self.asyncEcho(json.dumps(result))
        logger.info("End textDocument/signatureHelp")

    @args()
    def textDocument_codeAction(
            self, uri: str = None, languageId: str = None,
            range: Dict = None, context: Dict = None,
            cbs: List = None) -> None:
        logger.info("Begin textDocument/codeAction")

        self.textDocument_didChange()

        if cbs is None:
            cbs = [self.handleTextDocumentCodeActionResponse,
                   self.handleError]

        self.rpc[languageId].call("textDocument/codeAction", {
            "textDocument": uri,
            "range": range,
            "context": context,
        }, cbs)

    def handleTextDocumentCodeActionResponse(self, result):
        # TODO: proper integration.
        self.asyncEcho(json.dumps(result))
        logger.info("End textDocument/codeAction")

    def telemetry_event(self, params: Dict) -> None:
        if params.get("type") == "log":
            self.asyncEchomsg(params.get("message"))

    def window_logMessage(self, params: Dict) -> None:
        msgType = {
            1: "Error",
            2: "Warning",
            3: "Info",
            4: "Log",
        }[params["type"]]
        msg = "[{}] {}".format(msgType, params["message"])  # noqa: F841
        # self.asyncEchomsg(msg)

    # Extension in JDT language server.
    def language_status(self, params: Dict) -> None:
        msg = "{} {}".format(params["type"], params["message"])
        self.asyncEchomsg(msg)

    def handleRequestOrNotification(self, message) -> None:
        method = message['method'].replace('/', '_')
        if hasattr(self, method):
            try:
                getattr(self, method)(message['params'])
            except Exception:
                logger.exception("Exception in handle.")
        else:
            logger.warn('no handler implemented for ' + method)

    def handleError(self, message) -> None:
        self.asyncEcho(json.dumps(message))
