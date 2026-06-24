# encoding: ascii-8bit

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

require 'openc3/interfaces/interface'
require 'openc3/packets/packet'
require 'openc3/accessors/json_accessor'
require 'webrick'
require 'json'

module OpenC3
  # HTTP server interface that receives inbound JSON POSTs as telemetry.
  #
  # Path format: POST /targetname/packetname
  # The path segments are uppercased and used to identify the telemetry packet.
  # Item names in the packet definition must match the incoming JSON key names.
  # JsonAccessor is set automatically at connect time — no ACCESSOR keyword
  # needed in telemetry definitions.
  #
  # Supported options:
  #   LISTEN_ADDRESS <ip>  — bind address (default 0.0.0.0)
  class HttpJsonServerInterface < Interface
    OK_BODY = '{"status":"ok"}'.freeze
    OK_HEADERS = {
      'Content-Type'   => 'application/json',
      'Content-Length' => OK_BODY.bytesize.to_s,
      'Connection'     => 'close'
    }.freeze

    def initialize(port = 80)
      super()
      @listen_address = '0.0.0.0'
      @port = Integer(port)
      @server = nil
      @request_queue = Queue.new
    end

    # Supported Options
    # LISTEN_ADDRESS - IP address to bind (default 0.0.0.0)
    def set_option(option_name, option_values)
      super(option_name, option_values)
      @listen_address = option_values[0] if option_name.upcase == 'LISTEN_ADDRESS'
    end

    def connection_string
      "listening on #{@listen_address}:#{@port}"
    end

    def connect
      # Install JsonAccessor on every telemetry packet for our targets so
      # callers don't need ACCESSOR JsonAccessor in their .txt definitions.
      @target_names.each do |target_name|
        begin
          System.telemetry.packets(target_name).each do |_packet_name, packet|
            packet.accessor = JsonAccessor.new(packet)
          end
        rescue => e
          Logger.error("HttpJsonServerInterface: failed to set accessor for #{target_name}\n#{e.formatted}")
        end
      end

      @request_queue = Queue.new
      @server = WEBrick::HTTPServer.new(
        BindAddress: @listen_address,
        Port: @port,
        Logger: WEBrick::Log.new('/dev/null'),
        AccessLog: []
      )

      request_queue = @request_queue
      @server.mount_proc '/' do |req, res|
        parts = req.path.sub(%r{^/+}, '').sub(%r{/+$}, '').split('/')
        unless parts.length == 2
          res.status = 400
          res['Content-Type'] = 'application/json'
          body = JSON.generate({ status: 'error', message: "Path must be /target/packet, got: #{req.path}" })
          res.body = body
          next
        end

        target_name = parts[0].upcase
        packet_name = parts[1].upcase

        data = req.body.to_s.dup
        extra = { 'HTTP_REQUEST_TARGET_NAME' => target_name, 'HTTP_REQUEST_PACKET_NAME' => packet_name }
        extra['HTTP_HEADERS'] = req.header.transform_values { |v| v.is_a?(Array) ? v.first : v } if req.header
        extra['HTTP_QUERIES'] = req.query unless req.query.empty?

        request_queue << [data, extra]

        res.status = 200
        OK_HEADERS.each { |k, v| res[k] = v }
        res.body = OK_BODY
      end

      super()

      Thread.new { @server.start }
    end

    def connected?
      !@server.nil?
    end

    def disconnect
      @server&.shutdown
      @server = nil
      super()
      @request_queue << nil  # unblock read_interface
    end

    def read_interface
      item = @request_queue.pop
      return nil, nil if item.nil?
      data, extra = item
      read_interface_base(data, extra)
      [data, extra]
    end

    def write_interface(_data, _extra = nil)
      raise 'Commands cannot be sent to HttpJsonServerInterface'
    end

    def convert_packet_to_data(_packet)
      raise 'Commands cannot be sent to HttpJsonServerInterface'
    end

    def convert_data_to_packet(data, extra = nil)
      packet = Packet.new(nil, nil, :BIG_ENDIAN, nil, data.to_s)
      if extra
        target_name = extra.delete('HTTP_REQUEST_TARGET_NAME')
        packet_name = extra.delete('HTTP_REQUEST_PACKET_NAME')
        if target_name && packet_name
          packet.target_name = target_name.upcase
          packet.packet_name = packet_name.upcase
        end
        packet.extra = extra
      end
      packet
    end

    def details
      result = super()
      result['listen_address'] = @listen_address
      result['port'] = @port
      result['request_queue_length'] = @request_queue.length
      result
    end
  end
end
