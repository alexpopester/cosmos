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
  # JsonAccessor is set automatically on received packets — no ACCESSOR declaration
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
      @server_thread = nil
      @api_key = nil
      @max_queue_depth = 1000
      @allowed_content_types = ['application/json']
      @request_queue = Queue.new
    end

    # Supported Options
    # LISTEN_ADDRESS   - IP address to bind (default 0.0.0.0)
    # API_KEY          - Secret key callers must supply in X-Api-Key header
    # MAX_QUEUE_DEPTH  - Max inbound requests buffered before returning 503 (default 1000)
    # ALLOW_CONTENT_TYPE - Additional Content-Type to accept (default: application/json)
    def set_option(option_name, option_values)
      super(option_name, option_values)
      case option_name.upcase
      when 'LISTEN_ADDRESS'
        @listen_address = option_values[0]
      when 'API_KEY'
        @api_key = option_values[0]
      when 'MAX_QUEUE_DEPTH'
        @max_queue_depth = Integer(option_values[0])
      when 'ALLOW_CONTENT_TYPE'
        @allowed_content_types << option_values[0].downcase
      end
    end

    def connection_string
      "listening on #{@listen_address}:#{@port}"
    end

    def connect
      # Build the valid-packet lookup so the handler can reject unknown paths before 200 OK.
      # Also set JsonAccessor on every defined packet so the CVT singleton already has the
      # right accessor — ensures telemetry.update! and decom cloning both work without
      # requiring ACCESSOR declarations in the telemetry definitions.
      @valid_packets = {}
      @target_names.each do |target_name|
        begin
          System.telemetry.packets(target_name).each do |pkt_name, pkt|
            (@valid_packets[target_name] ||= {})[pkt_name] = true
            pkt.accessor = JsonAccessor.new(pkt)
          end
        rescue => e
          Logger.error("HttpJsonServerInterface: failed to enumerate packets for #{target_name}\n#{e.formatted}")
        end
      end

      @request_queue = SizedQueue.new(@max_queue_depth)
      @server = WEBrick::HTTPServer.new(
        BindAddress: @listen_address,
        Port: @port,
        Logger: WEBrick::Log.new('/dev/null'),
        AccessLog: []
      )

      @server.mount_proc '/' do |req, res|
        # 1. Authentication
        if @api_key && req['x-api-key'] != @api_key
          res.status = 401
          res['Content-Type'] = 'application/json'
          res.body = JSON.generate({ status: 'error', message: 'Unauthorized' })
          next
        end

        # 2. Path — expect /targetname/packetname
        parts = req.path.sub(%r{^/+}, '').sub(%r{/+$}, '').split('/')
        unless parts.length == 2
          res.status = 400
          res['Content-Type'] = 'application/json'
          res.body = JSON.generate({ status: 'error', message: "Path must be /target/packet, got: #{req.path}" })
          next
        end

        target_name = parts[0].upcase
        packet_name = parts[1].upcase

        # 3. Known packet validation
        unless @valid_packets.dig(target_name, packet_name)
          res.status = 404
          res['Content-Type'] = 'application/json'
          res.body = JSON.generate({ status: 'error', message: "Unknown packet: #{target_name}/#{packet_name}" })
          next
        end

        # 4. Content-Type enforcement
        content_type = req['content-type'].to_s.split(';').first.to_s.strip.downcase
        unless @allowed_content_types.include?(content_type)
          res.status = 415
          res['Content-Type'] = 'application/json'
          res.body = JSON.generate({
            status: 'error',
            message: "Unsupported Content-Type '#{content_type}', expected one of: #{@allowed_content_types.sort.join(', ')}"
          })
          next
        end

        data = req.body.to_s.dup
        extra = { 'HTTP_REQUEST_TARGET_NAME' => target_name, 'HTTP_REQUEST_PACKET_NAME' => packet_name }
        extra['HTTP_HEADERS'] = req.header.transform_values(&:first) if req.header
        extra['HTTP_QUERIES'] = req.query unless req.query.empty?

        # 5. Enqueue with backpressure
        begin
          @request_queue.push([data, extra], true)  # non_block: raises ThreadError when full
        rescue ThreadError
          res.status = 503
          res['Content-Type'] = 'application/json'
          res['Retry-After'] = '5'
          res.body = JSON.generate({ status: 'error', message: 'Server queue full — retry later' })
          next
        end

        res.status = 200
        OK_HEADERS.each { |k, v| res[k] = v }
        res.body = OK_BODY
      end

      @server_thread = Thread.new { @server.start }
      super()
    end

    def connected?
      !@server.nil?
    end

    def disconnect
      @server&.shutdown
      @server_thread&.join
      @server = nil
      @server_thread = nil
      super()
      # Drain first so the sentinel always fits in a bounded queue.
      @request_queue.clear
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
      packet.accessor = JsonAccessor.new(packet)
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
