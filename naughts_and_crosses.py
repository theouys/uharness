#!/usr/bin/env python3
"""Naughts and Crosses - a small, single-file tkinter game.

Play it:

    python3 naughts_and_crosses.py

Headless use (no window required):

    python3 naughts_and_crosses.py --selftest
    python3 naughts_and_crosses.py --moves 0 4 8 2 6

The rules live in `NaughtsAndCrosses`, which imports nothing and knows nothing
about tkinter, so it can be tested without a display. `GameApp` is the window.
"""

from __future__ import annotations

import sys

EMPTY = " "
NOUGHT = "O"
CROSS = "X"
DRAW = "draw"

# Every straight line of three cells, as indices into the flat 3x3 board:
#
#   0 | 1 | 2
#  ---+---+---
#   3 | 4 | 5
#  ---+---+---
#   6 | 7 | 8
WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),   # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),   # columns
    (0, 4, 8), (2, 4, 6),              # diagonals
)


class GameOverError(RuntimeError):
    """Raised when a move is attempted after the game has finished."""


class NaughtsAndCrosses:
    """The whole game, with no user interface attached.

    Turn order is X first, then O. After a winning move the game stops and
    `current_player` stays on the player who won, so the same attribute always
    says "who the board is waiting for".
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._cells = [EMPTY] * 9
        self.current_player = CROSS
        self.winner: str | None = None      # None, CROSS, NOUGHT or DRAW
        self.winning_line: tuple[int, int, int] | None = None

    @property
    def cells(self) -> tuple[str, ...]:
        """A copy of the board, reading left to right, top to bottom."""
        return tuple(self._cells)

    @property
    def is_over(self) -> bool:
        return self.winner is not None

    @property
    def is_draw(self) -> bool:
        return self.winner == DRAW

    def available_moves(self) -> list[int]:
        return [i for i, cell in enumerate(self._cells) if cell == EMPTY]

    def play(self, index: int) -> str:
        """Place the current player's mark on `index` and return that mark."""
        if self.is_over:
            raise GameOverError("the game is over; call reset() to play again")
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(f"move index must be an int, got {type(index).__name__}")
        if not 0 <= index <= 8:
            raise ValueError(f"move index must be 0-8, got {index}")
        if self._cells[index] != EMPTY:
            raise ValueError(f"cell {index} is already taken by {self._cells[index]!r}")

        mark = self.current_player
        self._cells[index] = mark
        self._update_state()

        if not self.is_over:
            self.current_player = NOUGHT if mark == CROSS else CROSS
        return mark

    def _update_state(self) -> None:
        for line in WIN_LINES:
            a, b, c = line
            if self._cells[a] != EMPTY and self._cells[a] == self._cells[b] == self._cells[c]:
                self.winner = self._cells[a]
                self.winning_line = line
                return
        if EMPTY not in self._cells:
            self.winner = DRAW
            self.winning_line = None


def render(cells) -> str:
    """ASCII picture of a board, for the command line."""
    rows = []
    for r in range(3):
        rows.append(" " + " | ".join(cells[r * 3 + c] for c in range(3)))
    return ("\n---+---+---\n").join(rows)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

WINDOW_BG = "#20242c"
CELL_BG = "#f2f4f8"
CELL_WIN_BG = "#ffe9a8"
X_COLOUR = "#2b6cb0"
O_COLOUR = "#c0392b"
TEXT_COLOUR = "#e8ecf3"


class GameApp:
    """A tkinter window around one `NaughtsAndCrosses` game."""

    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self._tk = tk
        self.root = root
        self.game = NaughtsAndCrosses()
        self.scores = {CROSS: 0, NOUGHT: 0, DRAW: 0}
        self._scored = False

        root.title("Naughts and Crosses")
        root.configure(bg=WINDOW_BG)
        root.resizable(False, False)

        self.status = tk.StringVar()
        self.score_text = tk.StringVar()

        base = tkfont.nametofont("TkDefaultFont")
        self._mark_font = (base.actual("family"), 44, "bold")

        tk.Label(
            root, textvariable=self.status, bg=WINDOW_BG, fg=TEXT_COLOUR,
            font=(base.actual("family"), 15, "bold"), pady=10,
        ).pack()

        grid = tk.Frame(root, bg=WINDOW_BG)
        grid.pack(padx=14)

        self.buttons: list[tk.Button] = []
        for index in range(9):
            button = tk.Button(
                grid, text="", width=2, height=1, font=self._mark_font,
                bg=CELL_BG, fg=WINDOW_BG, activebackground=CELL_BG,
                relief="flat", bd=0, highlightthickness=1,
                highlightbackground=WINDOW_BG, cursor="hand2",
                command=lambda i=index: self.on_cell_click(i),
            )
            button.grid(row=index // 3, column=index % 3, padx=3, pady=3, ipadx=6, ipady=2)
            self.buttons.append(button)

        tk.Label(
            root, textvariable=self.score_text, bg=WINDOW_BG, fg=TEXT_COLOUR,
            font=(base.actual("family"), 11), pady=8,
        ).pack()

        self.new_game_button = tk.Button(
            root, text="New game", command=self.new_game, relief="flat", bd=0,
            bg="#3b82f6", fg="white", activebackground="#2563eb",
            activeforeground="white", padx=16, pady=6, cursor="hand2",
        )
        self.new_game_button.pack(pady=(0, 14))

        # Number keys 1-9 play cells in reading order; Return starts a new game.
        for number, index in enumerate(range(9), start=1):
            root.bind(str(number), lambda _event, i=index: self.on_cell_click(i))
        root.bind("<Return>", lambda _event: self.new_game())

        self.refresh()

    # -- actions ----------------------------------------------------------

    def on_cell_click(self, index: int) -> None:
        if self.game.is_over:
            return
        try:
            self.game.play(index)
        except (ValueError, TypeError):
            return
        self.refresh()

    def new_game(self) -> None:
        self.game.reset()
        self._scored = False
        self.refresh()

    # -- drawing ----------------------------------------------------------

    def refresh(self) -> None:
        game = self.game
        if game.is_over and not self._scored:
            self.scores[DRAW if game.is_draw else game.winner] += 1
            self._scored = True

        for index, button in enumerate(self.buttons):
            mark = game.cells[index]
            button.configure(
                text="" if mark == EMPTY else mark,
                fg=X_COLOUR if mark == CROSS else O_COLOUR if mark == NOUGHT else CELL_BG,
                bg=CELL_WIN_BG if game.winning_line and index in game.winning_line else CELL_BG,
            )

        if game.is_draw:
            self.status.set("A draw - nobody wins")
        elif game.winner:
            self.status.set(f"{game.winner} wins!")
        else:
            self.status.set(f"{game.current_player} to play")

        self.score_text.set(
            f"X {self.scores[CROSS]}    O {self.scores[NOUGHT]}    "
            f"draws {self.scores[DRAW]}"
        )


def run_gui() -> int:
    import tkinter as tk

    root = tk.Tk()
    GameApp(root)
    root.mainloop()
    return 0


def selftest() -> int:
    """Check the rules without needing a display. Exit code 0 means all good."""
    checks = 0

    def check(condition, message):
        nonlocal checks
        if not condition:
            raise AssertionError(message)
        checks += 1

    def raises(exc_type, func, message):
        nonlocal checks
        try:
            func()
        except exc_type:
            checks += 1
            return
        except Exception as exc:                      # report unexpected errors
            raise AssertionError(f"{message}: raised {type(exc).__name__} instead") from exc
        raise AssertionError(f"{message}: nothing raised")

    game = NaughtsAndCrosses()
    check(game.cells == (EMPTY,) * 9, "a fresh board is empty")
    check(game.current_player == CROSS, "X moves first")
    check(game.available_moves() == list(range(9)), "every cell is available")
    check(not game.is_over and not game.is_draw, "a fresh game is not over")

    game.play(4)
    check(game.cells[4] == CROSS, "X lands where it was asked to")
    check(game.current_player == NOUGHT, "turn passes to O")
    game.play(0)
    check(game.current_player == CROSS, "turn passes back to X")

    raises(ValueError, lambda: game.play(4), "occupied cell is refused")
    raises(ValueError, lambda: game.play(9), "index above 8 is refused")
    raises(ValueError, lambda: game.play(-1), "negative index is refused")
    raises(TypeError, lambda: game.play("4"), "a non-integer move is refused")
    check(game.cells[4] == CROSS and game.cells.count(CROSS) == 1,
          "refused moves leave the board unchanged")

    # Row win for X: 0, 1, 2
    game = NaughtsAndCrosses()
    for move in (0, 3, 1, 4, 2):
        game.play(move)
    check(game.winner == CROSS, "three in a row wins")
    check(game.winning_line == (0, 1, 2), "the winning line is reported")
    check(game.is_over and not game.is_draw, "a win ends the game")
    check(game.current_player == CROSS, "the winner stays current_player")
    raises(GameOverError, lambda: game.play(8), "moves after a win are refused")

    # Column win for O: 1, 4, 7
    game = NaughtsAndCrosses()
    for move in (0, 1, 2, 4, 8, 7):
        game.play(move)
    check(game.winner == NOUGHT, "O can win a column")
    check(game.winning_line == (1, 4, 7), "the column line is reported")

    # Diagonal win for X: 2, 4, 6
    game = NaughtsAndCrosses()
    for move in (2, 0, 4, 1, 6):
        game.play(move)
    check(game.winner == CROSS, "X can win a diagonal")
    check(game.winning_line == (2, 4, 6), "the diagonal line is reported")

    # Draw: no line, board full
    game = NaughtsAndCrosses()
    for move in (0, 1, 2, 4, 3, 5, 7, 6, 8):
        game.play(move)
    check(game.is_draw, "a full board with no line is a draw")
    check(game.winner == DRAW and game.is_over, "a draw ends the game")
    check(game.winning_line is None, "a draw has no winning line")
    check(game.available_moves() == [], "a full board has no moves left")

    game.reset()
    check(game.cells == (EMPTY,) * 9, "reset clears the board")
    check(game.winner is None and game.current_player == CROSS, "reset clears the result")

    check(render(("X", " ", "O", " ", "X", " ", "O", " ", "X")).splitlines()[1]
          == "---+---+---", "render draws a separator row")
    print(f"selftest: {checks} checks passed")
    return 0


def play_moves(moves: list[str]) -> int:
    """Play a list of cell numbers on a fresh board and print the result."""
    game = NaughtsAndCrosses()
    for raw in moves:
        try:
            index = int(raw)
        except ValueError:
            print(f"error: {raw!r} is not a cell number", file=sys.stderr)
            return 2
        try:
            game.play(index)
        except (GameOverError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(render(game.cells))
            return 2
    print(render(game.cells))
    if game.is_draw:
        print("result: draw")
    elif game.winner:
        print(f"result: {game.winner} wins on line {list(game.winning_line)}")
    else:
        print(f"result: {game.current_player} to play")
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args[:1] == ["--selftest"]:
        return selftest()
    if args[:1] == ["--moves"]:
        if len(args) == 1:
            print("usage: naughts_and_crosses.py --moves 0 4 8 2 6", file=sys.stderr)
            return 2
        return play_moves(args[1:])
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__.strip())
        return 0
    if args:
        print(f"unknown argument: {args[0]} (try --help)", file=sys.stderr)
        return 2
    return run_gui()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
