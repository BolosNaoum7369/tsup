package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"gopkg.in/yaml.v3"
)

type clientConfig struct {
	Server       string `yaml:"server"`
	WSPort       int    `yaml:"ws_port"`
	TransferPort int    `yaml:"transfer_port"`
	Udid         string `yaml:"udid"`
	Token        string `yaml:"token"`
	Mappings     []struct {
		LanIP      string `yaml:"lan_ip"`
		LanPort    int    `yaml:"lan_port"`
		RemotePort int    `yaml:"remote_port"`
	} `yaml:"mappings"`
}

func main() {
	cfgPath := flag.String("config", "client.yaml", "config file")
	flag.Parse()

	data, err := os.ReadFile(*cfgPath)
	if err != nil {
		log.Fatal(err)
	}
	var cfg clientConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		log.Fatal(err)
	}
	if !validIPv4(cfg.Server) || !validPort(cfg.WSPort) || !validPort(cfg.TransferPort) ||
		!udidRe.MatchString(cfg.Udid) || !tokenRe.MatchString(cfg.Token) || len(cfg.Mappings) == 0 {
		log.Fatal("invalid config")
	}
	for _, m := range cfg.Mappings {
		if !validIPv4(m.LanIP) || !validPort(m.LanPort) {
			log.Fatal("invalid mapping")
		}
	}

	wsURL := fmt.Sprintf("ws://%s:%d", cfg.Server, cfg.WSPort)
	done := make(chan struct{})
	var wsMu sync.Mutex
	var ws *websocket.Conn

	go func() {
		for {
			select {
			case <-done:
				return
			default:
			}
			log.Printf("[Client] connecting %s", wsURL)
			conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
			if err != nil {
				log.Printf("[Client] connect failed: %v", err)
				time.Sleep(5 * time.Second)
				continue
			}
			conn.SetReadLimit(1 << 20)
			wsMu.Lock()
			ws = conn
			wsMu.Unlock()
			log.Println("[Client] connected")

			for _, m := range cfg.Mappings {
				_ = conn.WriteJSON(message{
					Type: msgAdd, Udid: cfg.Udid, Token: cfg.Token,
					LanIP: m.LanIP, LanPort: m.LanPort, RemotePort: m.RemotePort,
				})
			}

			for {
				select {
				case <-done:
					return
				default:
				}
				_, raw, err := conn.ReadMessage()
				if err != nil {
					log.Printf("[Client] disconnected: %v", err)
					wsMu.Lock()
					if ws == conn {
						ws = nil
					}
					wsMu.Unlock()
					break
				}
				var msg message
				if json.Unmarshal(raw, &msg) != nil {
					continue
				}
				switch msg.Type {
				case msgAddDone:
					log.Printf("[Mapping] %s:%d -> %s:%d", msg.LanIP, msg.LanPort, cfg.Server, msg.RemotePort)
				case msgReqTunnel:
					go openTunnel(cfg, msg)
				case msgError:
					log.Printf("[Server] %s", msg.Message)
				}
			}
			time.Sleep(5 * time.Second)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	close(done)
	wsMu.Lock()
	if ws != nil {
		ws.Close()
	}
	wsMu.Unlock()
}

func openTunnel(cfg clientConfig, msg message) {
	if len(msg.SessionID) != sessionIDLen || msg.LanIP == "" || msg.LanPort == 0 {
		return
	}
	transfer, err := net.Dial("tcp", fmt.Sprintf("%s:%d", cfg.Server, cfg.TransferPort))
	if err != nil {
		return
	}
	tuneTCP(transfer)
	if _, err := transfer.Write([]byte(msg.SessionID)); err != nil {
		transfer.Close()
		return
	}
	lan, err := net.Dial("tcp", fmt.Sprintf("%s:%d", msg.LanIP, msg.LanPort))
	if err != nil {
		transfer.Close()
		return
	}
	tuneTCP(lan)
	pipe(lan, transfer)
}
