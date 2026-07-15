package main

import (
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"gopkg.in/yaml.v3"
)

const (
	sessionTimeout   = 30 * time.Second
	cleanupInterval  = 5 * time.Second
	heartbeatEvery   = 5 * time.Second
	heartbeatMaxMiss = 2
)

type serverConfig struct {
	Listen struct {
		WS       int `yaml:"ws"`
		Transfer int `yaml:"transfer"`
	} `yaml:"listen"`
	Clients []struct {
		Udid  string `yaml:"udid"`
		Token string `yaml:"token"`
	} `yaml:"clients"`
}

type tcpSession struct {
	conn net.Conn
	ts   time.Time
}

type forwarder struct {
	udid, lanIP string
	lanPort     int
	port        int
	owner       uint64
	ln          net.Listener
}

type wsConn struct {
	conn        *websocket.Conn
	owner       uint64
	missedPongs int
	alive       bool
	mu          sync.Mutex
}

type tunnelServer struct {
	mu        sync.Mutex
	tokens    map[string]string
	sessions  map[string]*tcpSession
	fwd       map[string]*forwarder
	owners    map[uint64]*wsConn
	udidOwner map[string]uint64
	ownerSeq  uint64
}

func main() {
	cfgPath := flag.String("config", "server.yaml", "config file")
	flag.Parse()

	data, err := os.ReadFile(*cfgPath)
	if err != nil {
		log.Fatal(err)
	}
	var cfg serverConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		log.Fatal(err)
	}
	if !validPort(cfg.Listen.WS) || !validPort(cfg.Listen.Transfer) {
		log.Fatal("invalid listen ports")
	}
	tokens := make(map[string]string)
	for _, c := range cfg.Clients {
		if !udidRe.MatchString(c.Udid) || !tokenRe.MatchString(c.Token) {
			log.Fatal("invalid client config")
		}
		tokens[c.Udid] = c.Token
	}

	s := &tunnelServer{
		tokens: tokens,
		sessions: make(map[string]*tcpSession), fwd: make(map[string]*forwarder),
		owners: make(map[uint64]*wsConn), udidOwner: make(map[string]uint64),
	}
	go s.runTransfer(cfg.Listen.Transfer)
	go s.runControl(cfg.Listen.WS)
	go s.cleanupLoop()
	log.Printf("[Server] ws=%d transfer=%d", cfg.Listen.WS, cfg.Listen.Transfer)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
}

func (s *tunnelServer) runTransfer(port int) {
	ln, err := net.Listen("tcp", fmt.Sprintf("0.0.0.0:%d", port))
	if err != nil {
		log.Fatal(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go s.linkTransfer(c)
	}
}

func (s *tunnelServer) linkTransfer(backend net.Conn) {
	tuneTCP(backend)
	id := make([]byte, sessionIDLen)
	if _, err := io.ReadFull(backend, id); err != nil {
		backend.Close()
		return
	}
	s.mu.Lock()
	session, ok := s.sessions[string(id)]
	delete(s.sessions, string(id))
	s.mu.Unlock()
	if !ok {
		backend.Close()
		return
	}
	visitor := session.conn
	tuneTCP(visitor)
	pipe(visitor, backend)
}

func (s *tunnelServer) runControl(port int) {
	up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		owner := atomic.AddUint64(&s.ownerSeq, 1)
		ws := &wsConn{conn: c, owner: owner, alive: true}
		log.Printf("[Control] connected owner=%d", owner)
		go s.heartbeat(ws)
		s.serveWS(ws)
	})
	log.Fatal(http.ListenAndServe(fmt.Sprintf("0.0.0.0:%d", port), nil))
}

func (ws *wsConn) write(m message) {
	ws.mu.Lock()
	defer ws.mu.Unlock()
	if ws.conn != nil {
		_ = ws.conn.WriteJSON(m)
	}
}

func (s *tunnelServer) heartbeat(ws *wsConn) {
	ticker := time.NewTicker(heartbeatEvery)
	defer ticker.Stop()
	for range ticker.C {
		ws.mu.Lock()
		if ws.conn == nil {
			ws.mu.Unlock()
			return
		}
		if !ws.alive {
			ws.missedPongs++
			if ws.missedPongs >= heartbeatMaxMiss {
				_ = ws.conn.Close()
				ws.conn = nil
				ws.mu.Unlock()
				return
			}
		} else {
			ws.missedPongs = 0
		}
		ws.alive = false
		err := ws.conn.WriteMessage(websocket.PingMessage, nil)
		ws.mu.Unlock()
		if err != nil {
			return
		}
	}
}

func (s *tunnelServer) serveWS(ws *wsConn) {
	ws.conn.SetPongHandler(func(string) error {
		ws.mu.Lock()
		ws.alive = true
		ws.missedPongs = 0
		ws.mu.Unlock()
		return nil
	})
	defer func() {
		s.releaseOwner(ws.owner)
		s.closeFwd(func(f *forwarder) bool { return f.owner == ws.owner })
		ws.mu.Lock()
		if ws.conn != nil {
			ws.conn.Close()
			ws.conn = nil
		}
		ws.mu.Unlock()
	}()
	for {
		_, raw, err := ws.conn.ReadMessage()
		if err != nil {
			return
		}
		var msg message
		if json.Unmarshal(raw, &msg) != nil || msg.Type != msgAdd {
			continue
		}
		s.handleAdd(ws, msg)
	}
}

func (s *tunnelServer) handleAdd(ws *wsConn, msg message) {
	token, ok := s.tokens[msg.Udid]
	if !ok || !validIPv4(msg.LanIP) || !validPort(msg.LanPort) ||
		!tokenRe.MatchString(msg.Token) ||
		subtle.ConstantTimeCompare([]byte(token), []byte(msg.Token)) != 1 {
		ws.write(message{Type: msgError, Message: "unauthorized"})
		return
	}
	if msg.RemotePort != 0 && !validPort(msg.RemotePort) {
		ws.write(message{Type: msgError, Message: "invalid port"})
		return
	}

	s.mu.Lock()
	if prev := s.udidOwner[msg.Udid]; prev != 0 && prev != ws.owner {
		if prevWS := s.owners[prev]; prevWS != nil && prevWS.conn != nil {
			s.mu.Unlock()
			ws.write(message{Type: msgError, Message: "udid busy"})
			return
		}
	}
	s.mu.Unlock()

	s.claimUdid(msg.Udid, ws)

	id := mappingID(msg.Udid, msg.LanIP, msg.LanPort)
	s.closeFwd(func(f *forwarder) bool {
		return (f.udid == msg.Udid && f.owner != ws.owner) ||
			(f.udid == msg.Udid && f.lanIP == msg.LanIP && f.lanPort == msg.LanPort)
	})

	port := msg.RemotePort
	var ln net.Listener
	var err error
	if port == 0 {
		ln, err = net.Listen("tcp", "0.0.0.0:0")
		if err != nil {
			ws.write(message{Type: msgError, Message: "bind failed"})
			return
		}
		port = ln.Addr().(*net.TCPAddr).Port
	} else {
		for i := 0; i < 10; i++ {
			ln, err = net.Listen("tcp", fmt.Sprintf("0.0.0.0:%d", port))
			if err == nil {
				break
			}
			time.Sleep(200 * time.Millisecond)
		}
		if err != nil {
			ws.write(message{Type: msgError, Message: "bind failed"})
			return
		}
	}

	f := &forwarder{udid: msg.Udid, lanIP: msg.LanIP, lanPort: msg.LanPort, port: port, owner: ws.owner, ln: ln}
	s.mu.Lock()
	s.fwd[id] = f
	s.mu.Unlock()
	go s.accept(f, ws)

	ws.write(message{Type: msgAddDone, MappingID: id, LanIP: msg.LanIP, LanPort: msg.LanPort, RemotePort: port})
	log.Printf("[Mapping] %s:%d -> *:%d", msg.LanIP, msg.LanPort, port)
}

func (s *tunnelServer) accept(f *forwarder, ws *wsConn) {
	for {
		raw, err := f.ln.Accept()
		if err != nil {
			return
		}
		tuneTCP(raw)
		sid := uuid.New().String()
		s.mu.Lock()
		s.sessions[sid] = &tcpSession{conn: raw, ts: time.Now()}
		s.mu.Unlock()
		log.Printf("[Visitor] %s -> %s:%d session=%s", raw.RemoteAddr(), f.lanIP, f.lanPort, sid)
		ws.write(message{Type: msgReqTunnel, SessionID: sid, LanIP: f.lanIP, LanPort: f.lanPort})
	}
}

func (s *tunnelServer) claimUdid(udid string, ws *wsConn) {
	s.mu.Lock()
	prev := s.udidOwner[udid]
	s.udidOwner[udid] = ws.owner
	s.owners[ws.owner] = ws
	s.mu.Unlock()
	if prev != 0 && prev != ws.owner {
		s.closeFwd(func(f *forwarder) bool { return f.owner == prev })
		s.disconnectOwner(prev)
	}
}

func (s *tunnelServer) releaseOwner(owner uint64) {
	s.mu.Lock()
	delete(s.owners, owner)
	for u, o := range s.udidOwner {
		if o == owner {
			delete(s.udidOwner, u)
		}
	}
	s.mu.Unlock()
}

func (s *tunnelServer) disconnectOwner(owner uint64) {
	s.mu.Lock()
	ws := s.owners[owner]
	s.mu.Unlock()
	if ws == nil {
		return
	}
	ws.mu.Lock()
	if ws.conn != nil {
		_ = ws.conn.Close()
		ws.conn = nil
	}
	ws.mu.Unlock()
}

func (s *tunnelServer) closeFwd(match func(*forwarder) bool) {
	s.mu.Lock()
	var ids []string
	for id, f := range s.fwd {
		if match(f) {
			ids = append(ids, id)
		}
	}
	for _, id := range ids {
		if f := s.fwd[id]; f != nil && f.ln != nil {
			f.ln.Close()
		}
		delete(s.fwd, id)
	}
	s.mu.Unlock()
}

func (s *tunnelServer) cleanupLoop() {
	ticker := time.NewTicker(cleanupInterval)
	defer ticker.Stop()
	for range ticker.C {
		now := time.Now()
		s.mu.Lock()
		for sid, sess := range s.sessions {
			if now.Sub(sess.ts) > sessionTimeout {
				sess.conn.Close()
				delete(s.sessions, sid)
			}
		}
		s.mu.Unlock()
	}
}
