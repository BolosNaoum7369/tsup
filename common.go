package main

import (
	"fmt"
	"io"
	"net"
	"regexp"
	"time"
)

const sessionIDLen = 36

var (
	udidRe  = regexp.MustCompile(`^[0-9a-f]{24}$`)
	tokenRe = regexp.MustCompile(`^[^\s]{1,128}$`)
)

type msgType string

const (
	msgAdd       msgType = "ADD"
	msgAddDone   msgType = "ADD_DONE"
	msgReqTunnel msgType = "REQ_TUNNEL"
	msgError     msgType = "ERROR"
)

type message struct {
	Type       msgType `json:"type"`
	MappingID  string  `json:"mappingId,omitempty"`
	Udid       string  `json:"udid,omitempty"`
	Token      string  `json:"token,omitempty"`
	LanIP      string  `json:"lanIp,omitempty"`
	LanPort    int     `json:"lanPort,omitempty"`
	RemotePort int     `json:"remotePort,omitempty"`
	SessionID  string  `json:"sessionId,omitempty"`
	Message    string  `json:"message,omitempty"`
}

func validPort(p int) bool { return p >= 1 && p <= 65535 }

func validIPv4(s string) bool {
	ip := net.ParseIP(s)
	return ip != nil && ip.To4() != nil
}

func mappingID(udid, lanIP string, lanPort int) string {
	return fmt.Sprintf("tcp:%s:%s:%d", udid, lanIP, lanPort)
}

func tuneTCP(c net.Conn) {
	if tc, ok := c.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
		tc.SetKeepAlive(true)
		tc.SetKeepAlivePeriod(5 * time.Second)
	}
}

func pipe(a, b net.Conn) {
	go func() { io.Copy(b, a); b.Close() }()
	io.Copy(a, b)
	a.Close()
}
