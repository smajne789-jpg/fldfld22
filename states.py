from aiogram.fsm.state import State, StatesGroup


class DepositFlow(StatesGroup):
    amount = State()


class WithdrawFlow(StatesGroup):
    amount = State()


class BetFlow(StatesGroup):
    amount = State()


class AdminReserveFlow(StatesGroup):
    amount = State()


class AdminCheckFlow(StatesGroup):
    amount = State()
    activations = State()
    min_deposits = State()
