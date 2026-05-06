//+------------------------------------------------------------------+
//|  ApexReceiver.mq5                                                |
//|  Apex EA — MT5 Execution Wrapper                                 |
//|                                                                  |
//|  ROLE: Thin, dumb, reliable execution layer only.               |
//|  All intelligence lives in the Python brain.                    |
//|  This file: receives JSON signals via ZeroMQ, routes orders,    |
//|  enforces hard prop firm risk rules, emits trade results back.  |
//|                                                                  |
//|  Dependencies:                                                   |
//|    - DWX_ZeroMQ connector (place .mqh files in MQL5/Include/)   |
//|      https://github.com/darwinex/dwx-zeromq-connector           |
//+------------------------------------------------------------------+
#property copyright "Apex EA"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <DWX_ZeroMQ_Connector.mqh>   // from darwinex repo

//── Input parameters ────────────────────────────────────────────────────────
input int    MAGIC_NUMBER           = 20260101;
input string SYMBOL                 = "EURUSD";
input int    ZMQ_SIGNAL_PORT        = 5555;    // receives signals FROM Python
input int    ZMQ_DATA_PORT          = 5556;    // sends results  TO   Python

//── Prop firm hard limits (enforced here independently of Python) ────────────
input double MAX_DAILY_DD_PCT       = 3.0;     // % — daily halt
input double MAX_ACCOUNT_DD_PCT     = 8.0;     // % — permanent halt
input int    MAX_TRADES_PER_DAY     = 3;
input int    MAX_CONSECUTIVE_LOSSES = 2;
input string EOD_CLOSE_TIME         = "23:30"; // NY time HH:MM

//── Internal state ───────────────────────────────────────────────────────────
CTrade          trade;
CPositionInfo   pos;
DWX_ZeroMQ_Connector* zmq;

double   g_starting_balance    = 0;
double   g_day_start_equity    = 0;
int      g_trades_today        = 0;
int      g_consecutive_losses  = 0;
bool     g_daily_halt          = false;
bool     g_permanent_halt      = false;
datetime g_last_eod_check      = 0;
datetime g_last_hb             = 0;         // last Python heartbeat received


//+------------------------------------------------------------------+
//| Expert initialisation                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    trade.SetExpertMagicNumber(MAGIC_NUMBER);
    trade.SetDeviationInPoints(10);
    trade.SetTypeFilling(ORDER_FILLING_FOK);

    g_starting_balance = AccountInfoDouble(ACCOUNT_BALANCE);
    g_day_start_equity = AccountInfoDouble(ACCOUNT_EQUITY);

    // Initialise ZeroMQ connector
    // SUB on ZMQ_SIGNAL_PORT (receive from Python)
    // PUB on ZMQ_DATA_PORT   (send to Python)
    zmq = new DWX_ZeroMQ_Connector(ZMQ_SIGNAL_PORT, ZMQ_DATA_PORT);
    if(zmq == NULL)
    {
        Print("APEX | ERROR: Failed to create ZMQ connector.");
        return INIT_FAILED;
    }

    PrintFormat("APEX | Initialised | Magic=%d | Balance=%.2f",
                MAGIC_NUMBER, g_starting_balance);
    return INIT_SUCCEEDED;
}


//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    if(zmq != NULL) { delete zmq; zmq = NULL; }
    Print("APEX | Deinitialised.");
}


//+------------------------------------------------------------------+
//| Main tick loop                                                   |
//+------------------------------------------------------------------+
void OnTick()
{
    // ── Check end-of-day close ────────────────────────────────────────────
    if(IsEODCloseTime())
    {
        CloseAllPositions("eod");
        ResetDailyCounters();
        return;
    }

    // ── Check permanent halt ──────────────────────────────────────────────
    if(g_permanent_halt)
    {
        static bool halt_warned = false;
        if(!halt_warned) {
            Print("APEX | PERMANENT HALT — account drawdown limit reached.");
            halt_warned = true;
        }
        return;
    }

    // ── Check daily halt ─────────────────────────────────────────────────
    CheckDrawdownLimits();
    if(g_daily_halt) return;

    // ── Poll ZMQ for incoming signal ──────────────────────────────────────
    string raw_msg = zmq.GetLatestMessage();
    if(StringLen(raw_msg) == 0) return;

    // Parse JSON signal
    string action    = ParseJsonString(raw_msg, "action");
    string symbol    = ParseJsonString(raw_msg, "symbol");
    double lots      = ParseJsonDouble(raw_msg, "lots");
    double sl_price  = ParseJsonDouble(raw_msg, "sl_price");
    double tp_price  = ParseJsonDouble(raw_msg, "tp_price");
    long   ticket    = (long)ParseJsonDouble(raw_msg, "ticket");
    string tier      = ParseJsonString(raw_msg, "tier");
    string strategy  = ParseJsonString(raw_msg, "strategy");
    string comment   = ParseJsonString(raw_msg, "comment");
    int    magic_in  = (int)ParseJsonDouble(raw_msg, "magic");

    // Ignore signals not from our Python instance
    if(magic_in != MAGIC_NUMBER) return;

    // Heartbeat — Python is alive
    if(action == "HEARTBEAT")
    {
        g_last_hb = TimeCurrent();
        return;
    }

    // ── Route by action ───────────────────────────────────────────────────
    if(action == "BUY"  || action == "SELL")     ExecuteEntry(action, symbol, lots, sl_price, tp_price, strategy, tier, comment);
    else if(action == "CLOSE")                   ExecuteClose(ticket, comment);
    else if(action == "CLOSE_ALL")               CloseAllPositions(comment);
    else PrintFormat("APEX | Unknown action: %s", action);
}


//+------------------------------------------------------------------+
//| Execute an entry order                                           |
//+------------------------------------------------------------------+
void ExecuteEntry(
    string action, string sym, double lots,
    double sl, double tp, string strategy, string tier, string cmt)
{
    // ── Pre-trade rule checks ──────────────────────────────────────────────
    if(g_trades_today >= MAX_TRADES_PER_DAY) {
        PrintFormat("APEX | BLOCKED | Max trades/day reached (%d)", MAX_TRADES_PER_DAY);
        EmitResult("SIGNAL_BLOCKED", "max_trades_day", 0, 0);
        return;
    }
    if(g_consecutive_losses >= MAX_CONSECUTIVE_LOSSES) {
        PrintFormat("APEX | BLOCKED | Max consecutive losses (%d)", MAX_CONSECUTIVE_LOSSES);
        EmitResult("SIGNAL_BLOCKED", "consecutive_losses", 0, 0);
        return;
    }
    if(g_daily_halt || g_permanent_halt) {
        EmitResult("SIGNAL_BLOCKED", "halt_active", 0, 0);
        return;
    }

    // ── Place order ────────────────────────────────────────────────────────
    string full_comment = StringFormat("%s_%s_%s", strategy, tier, cmt);
    bool   result       = false;

    if(action == "BUY")
        result = trade.Buy(lots, sym, 0, sl, tp, full_comment);
    else if(action == "SELL")
        result = trade.Sell(lots, sym, 0, sl, tp, full_comment);

    if(result) {
        g_trades_today++;
        ulong  new_ticket = trade.ResultOrder();
        double fill_price = trade.ResultPrice();
        PrintFormat("APEX | %s | ticket=%llu | lots=%.2f | price=%.5f | SL=%.5f | TP=%.5f",
                    action, new_ticket, lots, fill_price, sl, tp);
        EmitResult("TRADE_OPENED", action, (long)new_ticket, fill_price);
    } else {
        PrintFormat("APEX | ORDER FAILED | %s | retcode=%d | %s",
                    action, trade.ResultRetcode(), trade.ResultComment());
        EmitResult("ORDER_FAILED", action, 0, 0);
    }
}


//+------------------------------------------------------------------+
//| Close a specific position by ticket                              |
//+------------------------------------------------------------------+
void ExecuteClose(long ticket, string reason)
{
    if(pos.SelectByTicket((ulong)ticket)) {
        trade.PositionClose((ulong)ticket);
        PrintFormat("APEX | CLOSED | ticket=%lld | reason=%s", ticket, reason);
    } else {
        PrintFormat("APEX | CLOSE FAILED | ticket=%lld not found", ticket);
    }
}


//+------------------------------------------------------------------+
//| Close all open positions                                         |
//+------------------------------------------------------------------+
void CloseAllPositions(string reason)
{
    for(int i = PositionsTotal() - 1; i >= 0; i--) {
        if(pos.SelectByIndex(i)) {
            if(pos.Magic() == MAGIC_NUMBER && pos.Symbol() == SYMBOL) {
                trade.PositionClose(pos.Ticket());
                PrintFormat("APEX | CLOSED ALL | ticket=%llu | reason=%s",
                            pos.Ticket(), reason);
            }
        }
    }
}


//+------------------------------------------------------------------+
//| Trade transaction hook — fires when a trade closes              |
//+------------------------------------------------------------------+
void OnTradeTransaction(
    const MqlTradeTransaction& trans,
    const MqlTradeRequest&     req,
    const MqlTradeResult&      res)
{
    // Only care about deal-add events (position closing)
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;

    ulong deal_ticket = trans.deal;
    if(deal_ticket == 0) return;

    // Select the deal from history
    if(!HistoryDealSelect(deal_ticket)) return;
    if(HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != MAGIC_NUMBER) return;
    if(HistoryDealGetInteger(deal_ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT) return;

    double profit     = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
    double commission = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
    double swap       = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
    double net        = profit + commission + swap;
    string sym        = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
    string cmt        = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
    long   order      = (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
    double close_px   = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);

    // Update consecutive loss counter
    if(net < 0) g_consecutive_losses++;
    else        g_consecutive_losses = 0;

    PrintFormat("APEX | TRADE CLOSED | deal=%llu | net=%.2f | losses_streak=%d",
                deal_ticket, net, g_consecutive_losses);

    // Emit full result to Python
    string payload = StringFormat(
        "{\"event\":\"TRADE_CLOSED\","
        "\"ticket\":%llu,"
        "\"order\":%ld,"
        "\"symbol\":\"%s\","
        "\"close_price\":%.5f,"
        "\"profit\":%.2f,"
        "\"commission\":%.2f,"
        "\"swap\":%.2f,"
        "\"net_profit\":%.2f,"
        "\"magic\":%d,"
        "\"comment\":\"%s\","
        "\"close_time\":\"%s\"}",
        deal_ticket, order, sym, close_px,
        profit, commission, swap, net,
        MAGIC_NUMBER, cmt,
        TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS)
    );
    zmq.SendMessage(payload);
}


//+------------------------------------------------------------------+
//| Drawdown guard                                                   |
//+------------------------------------------------------------------+
void CheckDrawdownLimits()
{
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
    if(balance == 0) return;

    double dd_from_balance   = (balance - equity) / balance * 100.0;
    double dd_from_day_start = (g_day_start_equity - equity) / g_day_start_equity * 100.0;

    // Permanent halt: 8% from account balance
    if(dd_from_balance >= MAX_ACCOUNT_DD_PCT && !g_permanent_halt) {
        g_permanent_halt = true;
        CloseAllPositions("permanent_halt");
        Print("APEX | *** PERMANENT HALT — 8% account drawdown reached ***");
        EmitResult("PERMANENT_HALT", "account_dd", 0, 0);
    }

    // Daily halt: 3% from day-start equity
    if(dd_from_day_start >= MAX_DAILY_DD_PCT && !g_daily_halt) {
        g_daily_halt = true;
        CloseAllPositions("daily_halt");
        PrintFormat("APEX | DAILY HALT | DD=%.2f%%", dd_from_day_start);
        EmitResult("DAILY_HALT", "daily_dd", 0, 0);
    }
}


//+------------------------------------------------------------------+
//| EOD close check                                                  |
//+------------------------------------------------------------------+
bool IsEODCloseTime()
{
    MqlDateTime now;
    TimeToStruct(TimeGMT(), now);           // adjust to NY (-5 or -4 in DST)
    int ny_hour   = (now.hour - 5 + 24) % 24;    // rough NY offset, no DST
    int ny_minute = now.min;
    return (ny_hour == 23 && ny_minute >= 30);
}


//+------------------------------------------------------------------+
//| Reset daily counters at midnight UTC                             |
//+------------------------------------------------------------------+
void ResetDailyCounters()
{
    g_trades_today       = 0;
    g_consecutive_losses = 0;
    g_daily_halt         = false;
    g_day_start_equity   = AccountInfoDouble(ACCOUNT_EQUITY);
    Print("APEX | Daily counters reset.");
}


//+------------------------------------------------------------------+
//| Emit a result/event message back to Python via ZMQ              |
//+------------------------------------------------------------------+
void EmitResult(string event, string detail, long ticket, double price)
{
    string payload = StringFormat(
        "{\"event\":\"%s\",\"detail\":\"%s\","
        "\"ticket\":%ld,\"price\":%.5f,"
        "\"magic\":%d,\"ts\":\"%s\"}",
        event, detail, ticket, price,
        MAGIC_NUMBER,
        TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS)
    );
    zmq.SendMessage(payload);
}


//+------------------------------------------------------------------+
//| Minimal JSON field parsers                                       |
//| (replace with a proper JSON lib if available in your MT5 build) |
//+------------------------------------------------------------------+
string ParseJsonString(string json, string key)
{
    string search = "\"" + key + "\":\"";
    int    start  = StringFind(json, search);
    if(start < 0) return "";
    start += StringLen(search);
    int end = StringFind(json, "\"", start);
    if(end < 0) return "";
    return StringSubstr(json, start, end - start);
}

double ParseJsonDouble(string json, string key)
{
    string search = "\"" + key + "\":";
    int    start  = StringFind(json, search);
    if(start < 0) return 0;
    start += StringLen(search);
    // read until comma, }, or end
    string val = "";
    for(int i = start; i < StringLen(json); i++) {
        ushort c = StringGetCharacter(json, i);
        if(c == ',' || c == '}' || c == ' ') break;
        val += ShortToString(c);
    }
    return StringToDouble(val);
}
